"""
Lógica de scoring de calidad del SupervisorAgent.
Migrado desde backend-ia/agents/supervisor.py.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, ValidationError, field_validator

from agents.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT
from observability.metrics import (
    LLM_TOKENS_TOTAL,
    SUPERVISOR_DECISIONS_TOTAL,
    SUPERVISOR_LATENCY_SECONDS,
    SUPERVISOR_QUALITY_SCORE,
    SUPERVISOR_REPLAN_TOTAL,
)
from observability.tracing import tracer

logger = logging.getLogger("agents.supervisor.scorer")

MAX_RETRIES = 1
_FEEDBACK_KEY_PREFIX = "agent_feedback:"
_FEEDBACK_MAX_ENTRIES = 100

# Singleton LLM (temperatura 0 para evaluación determinística)
_chat_llm: ChatOllama | None = None


def _get_llm() -> ChatOllama:
    global _chat_llm
    if _chat_llm is None:
        from config.settings import settings
        _chat_llm = ChatOllama(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
    return _chat_llm


class SupervisorDecisionModel(BaseModel):
    decision: Literal["ACCEPT", "REPLAN"]
    quality_score: int
    reason: str

    @field_validator("quality_score")
    @classmethod
    def clamp_score(cls, v: int) -> int:
        return max(0, min(10, v))


def _parse_decision(raw: str) -> SupervisorDecisionModel:
    """Parsea JSON del LLM con fallback heurístico."""
    match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return SupervisorDecisionModel(**data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(f"[supervisor] Parse error: {exc}. Raw: {raw!r}")
    if "replan" in raw.lower() or "rechaz" in raw.lower():
        return SupervisorDecisionModel(
            decision="REPLAN", quality_score=3, reason="Heuristic REPLAN"
        )
    return SupervisorDecisionModel(
        decision="ACCEPT", quality_score=6, reason="Heuristic ACCEPT"
    )


def _build_update(decision: str, score: int, reason: str, retry_count: int) -> dict:
    SUPERVISOR_DECISIONS_TOTAL.labels(decision=decision).inc()
    SUPERVISOR_QUALITY_SCORE.observe(score)
    if decision == "REPLAN":
        SUPERVISOR_REPLAN_TOTAL.inc()
    return {
        "supervisor_score": score,
        "supervisor_reason": reason,
        "supervisor_decision": decision,
        "retry_count": retry_count + (1 if decision == "REPLAN" else 0),
    }


def _record_feedback(
    decision: str,
    score: int,
    reason: str,
    question: str,
    plan: list,
    user_id: str,
    redis_client,
) -> None:
    """Persiste entrada de feedback en Redis (non-blocking)."""
    if redis_client is None:
        return
    try:
        import json as _json
        entry = _json.dumps({
            "ts": time.time(),
            "user_id": user_id,
            "question": question[:200],
            "plan": plan,
            "score": score,
            "decision": decision,
            "reason": reason,
        })
        key = f"{_FEEDBACK_KEY_PREFIX}{user_id}"
        redis_client.lpush(key, entry)
        redis_client.ltrim(key, 0, _FEEDBACK_MAX_ENTRIES - 1)
    except Exception as exc:
        logger.warning(f"[supervisor] No se pudo guardar feedback en Redis: {exc}")


def evaluate(state: dict, redis_client=None) -> dict:
    """
    Evalúa la calidad de la respuesta final y retorna el state update.
    Función principal del supervisor — compatible con el nodo LangGraph.
    """
    with tracer.start_as_current_span("agent.supervisor") as span:
        question = state.get("question", "")
        final_answer = state.get("final_answer", "") or ""
        plan = state.get("plan", [])
        retry_count = state.get("retry_count", 0)
        user_id = state.get("user_id", "unknown")

        span.set_attribute("agent.user_id", user_id)
        span.set_attribute("agent.retry_count", retry_count)

        # Fast-path: respuesta vacía
        if not final_answer.strip():
            logger.warning("[supervisor] Respuesta vacía detectada.")
            decision = "REPLAN" if retry_count < MAX_RETRIES else "ACCEPT"
            _record_feedback(decision, 0, "La respuesta está vacía.", question, plan, user_id, redis_client)
            return _build_update(decision, 0, "La respuesta está vacía.", retry_count)

        # Evaluación LLM
        evaluation_prompt = (
            f"PREGUNTA DEL USUARIO:\n{question}\n\n"
            f"RESPUESTA GENERADA:\n{final_answer[:2000]}"
        )
        messages = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=evaluation_prompt),
        ]

        t0 = time.time()
        try:
            _sv_resp = _get_llm().invoke(messages)
            SUPERVISOR_LATENCY_SECONDS.observe(time.time() - t0)
            raw = (_sv_resp.content if hasattr(_sv_resp, "content") else str(_sv_resp)).strip()
            try:
                from config.settings import settings as _s
                _model = _s.llm_model
                _usage = getattr(_sv_resp, "usage_metadata", None)
                if _usage:
                    LLM_TOKENS_TOTAL.labels(model=_model, token_type="input").inc(_usage.get("input_tokens", 0))
                    LLM_TOKENS_TOTAL.labels(model=_model, token_type="output").inc(_usage.get("output_tokens", 0))
                else:
                    LLM_TOKENS_TOTAL.labels(model=_model, token_type="input").inc(len(evaluation_prompt) // 4)
                    LLM_TOKENS_TOTAL.labels(model=_model, token_type="output").inc(len(raw) // 4)
            except Exception:
                pass
            sv_decision = _parse_decision(raw)
        except Exception as exc:
            logger.error(f"[supervisor] Error invocando LLM: {exc}")
            SUPERVISOR_LATENCY_SECONDS.observe(time.time() - t0)
            sv_decision = SupervisorDecisionModel(
                decision="ACCEPT", quality_score=5,
                reason=f"Supervisor error (fail-open): {exc}",
            )

        span.set_attribute("agent.supervisor_decision", sv_decision.decision)
        span.set_attribute("agent.supervisor_score", sv_decision.quality_score)

        # Forzar ACCEPT si se alcanzó el límite de reintentos
        decision = sv_decision.decision
        if decision == "REPLAN" and retry_count >= MAX_RETRIES:
            logger.warning(
                f"[supervisor] REPLAN solicitado pero retry_count={retry_count} "
                f">= MAX_RETRIES={MAX_RETRIES}. Forzando ACCEPT."
            )
            decision = "ACCEPT"

        _record_feedback(
            decision, sv_decision.quality_score, sv_decision.reason,
            question, plan, user_id, redis_client,
        )
        logger.info(
            f"[supervisor] decision={decision} score={sv_decision.quality_score} "
            f"retry={retry_count} reason={sv_decision.reason!r}"
        )
        return _build_update(decision, sv_decision.quality_score, sv_decision.reason, retry_count)
