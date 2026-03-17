"""
PlannerAgent — descompone la pregunta del usuario en un plan de ejecución.

Migrado desde backend-ia/agents/planner.py.
Cambios respecto al original:
  - Imports actualizados a la nueva estructura (observability/, config/, core/)
  - Singleton LLM preservado a nivel de módulo (performance crítico)
  - Funciones de nodo LangGraph expuestas como métodos estáticos para compatibilidad
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import ValidationError

from agents.base.agent_registry import AgentRegistry
from agents.planner.grouper import group_plan_into_batches
from agents.planner.models import PlanStep
from agents.planner.prompts import PLANNER_SYSTEM_PROMPT
from core.agent_base import AgentContext, AgentResult, BaseAgent
from core.constants import MAX_PLAN_STEPS
from observability.metrics import (
    PLANNER_INVALID_STEPS_TOTAL,
    PLANNER_LATENCY_SECONDS,
    PLANNER_PARSE_ERRORS_TOTAL,
    PLANNER_PLAN_SIZE,
    PLANNER_STEP_TYPES_TOTAL,
)
from observability.tracing import tracer

logger = logging.getLogger("agents.planner")

# ── Singleton LLM a nivel de módulo (no se reinstancia por request) ────────────
_llm: ChatOllama | None = None


def _get_llm() -> ChatOllama:
    global _llm
    if _llm is None:
        from config.settings import settings
        _llm = ChatOllama(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
        )
        logger.info(
            f"[planner] ChatOllama inicializado: "
            f"{settings.llm_model} @ {settings.ollama_base_url}"
        )
    return _llm


# ── Helpers (compatibles con el código original) ───────────────────────────────

def _parse_raw_response(response: str) -> List[str]:
    """Extrae una lista JSON de la respuesta cruda del LLM."""
    if response.startswith("[") and response.endswith("]"):
        return json.loads(response)
    match = re.search(r"\[.*?\]", response, re.DOTALL)
    if match:
        return json.loads(match.group())
    return [f"REASONING: {response}"]


def _validate_plan(raw_steps: list) -> List[str]:
    """Valida cada paso con Pydantic y aplica el cap MAX_PLAN_STEPS."""
    validated: List[str] = []
    for raw in raw_steps:
        if not isinstance(raw, str):
            PLANNER_INVALID_STEPS_TOTAL.inc()
            continue
        try:
            step = PlanStep.from_string(raw)
            validated.append(step.to_string())
            PLANNER_STEP_TYPES_TOTAL.labels(step_type=step.step_type).inc()
        except (ValidationError, ValueError) as exc:
            PLANNER_INVALID_STEPS_TOTAL.inc()
            logger.warning(f"[planner] Paso inválido descartado {raw!r}: {exc}")
    return validated[:MAX_PLAN_STEPS]


def _apply_fast_paths(question: str, plan: List[str]) -> List[str]:
    """
    Fast-paths para peticiones que no necesitan pasar por ReAct completo.
    Preservado del comportamiento original.
    """
    q_lower = question.lower()
    if any(kw in q_lower for kw in ("grafana", "imagen", "dashboard", "consumo")):
        if "rag" in q_lower or "performance" in q_lower:
            return [
                "K8S_TOOL: rag",
                "REASONING: Indicar brevemente al usuario que la captura del "
                "dashboard RAG Performance está adjunta como imagen",
            ]
        return [
            "K8S_TOOL: recursos",
            "REASONING: Indicar brevemente al usuario que la captura del "
            "dashboard de recursos del clúster está adjunta como imagen",
        ]
    return plan


# ── Nodo LangGraph (función pura, compatible con el workflow original) ─────────

def planner_node(state: Dict[str, Any], llm=None) -> Dict[str, Any]:
    """
    Nodo LangGraph: genera el plan de ejecución.

    Seguridad: usa SystemMessage/HumanMessage por separado para que el input
    del usuario NUNCA toque el system prompt (prevención de prompt injection).
    """
    with tracer.start_as_current_span("agent.planner") as span:
        question = state["question"]
        span.set_attribute("agent.question_length", len(question))
        span.set_attribute("agent.user_id", state.get("user_id", "unknown"))

        messages = [
            SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            HumanMessage(content=question),
        ]

        t0 = time.time()
        response = _get_llm().invoke(messages).content.strip()
        PLANNER_LATENCY_SECONDS.observe(time.time() - t0)

        plan: List[str] = []
        try:
            raw_steps = _parse_raw_response(response)
            plan = _validate_plan(raw_steps)
        except Exception as exc:
            PLANNER_PARSE_ERRORS_TOTAL.inc()
            logger.error(
                f"[planner] Error de parseo: {exc}. "
                f"Respuesta cruda: {response!r}"
            )
            plan = ["REASONING: Responder la consulta del usuario de forma general."]

        if not plan:
            plan = ["REASONING: Responder la consulta del usuario de forma general."]

        plan = _apply_fast_paths(question, plan)

        PLANNER_PLAN_SIZE.observe(len(plan))
        span.set_attribute("agent.plan_steps", len(plan))
        span.set_attribute("agent.plan", str(plan))
        logger.info(f"[planner] Plan generado ({len(plan)} pasos): {plan}")

        return {**state, "plan": plan, "current_step": 0}


def grouper_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Nodo LangGraph: convierte el plan plano en batches paralelos."""
    batches = group_plan_into_batches(state.get("plan", []))
    return {**state, "batches": batches, "current_batch": 0}


# ── BaseAgent wrapper (nueva arquitectura) ────────────────────────────────────

@AgentRegistry.register
class SarielAgent(BaseAgent):
    """
    Sariel — Planner Agent: descompone un request en pasos ejecutables.

    Se puede usar tanto como nodo LangGraph (via planner_node) como
    agente standalone (via run()).
    """

    name = "sariel"
    role = "Descomponer requests en planes de ejecución paso a paso"
    version = "2.0.0"
    capabilities = ["task_decomposition", "step_planning", "batch_grouping"]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        question = task.get("question", "")
        user_id = task.get("user_id", "unknown")

        state = {"question": question, "user_id": user_id}
        result = planner_node(state)
        plan = result.get("plan", [])
        batches = group_plan_into_batches(plan)

        return AgentResult(
            success=True,
            output={"plan": plan, "batches": batches},
            agent_name=self.name,
        )
