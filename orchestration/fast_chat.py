"""
fast_chat — ruta rápida para charla simple (saludos, agradecimientos, respuestas cortas).

En vez de disparar el pipeline completo (planner → executor → supervisor) con qwen3:14b,
resuelve el mensaje con UNA sola llamada a un modelo chico (qwen3:1.7b por defecto) que
está keep_alive en VRAM. Latencia esperada ~1-2s vs ~20s+ del pipeline.

El router marca intent="chat" solo para mensajes claramente sociales; cualquier cosa
técnica o analítica sigue yendo al pipeline con qwen3:14b.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

logger = logging.getLogger("orchestration.fast_chat")

_fast_llm = None
_lock = threading.Lock()

_SYSTEM_PROMPT = (
    "Eres Amael, un asistente personal en español de México. "
    "Responde de forma breve, cálida y natural, sin tecnicismos innecesarios. "
    "Si el usuario solo saluda o agradece, responde con naturalidad y ofrece ayuda "
    "en una frase. No inventes datos ni ejecutes acciones."
)


def _get_fast_llm():
    """Singleton del modelo chico. keep_alive=-1 lo mantiene en VRAM; think=False
    evita el modo thinking de qwen3 (que bloquearía la llamada generando <think>)."""
    global _fast_llm
    if _fast_llm is None:
        with _lock:
            if _fast_llm is None:
                from config.settings import settings
                from langchain_ollama import ChatOllama

                _fast_llm = ChatOllama(
                    model=settings.llm_model_fast,
                    base_url=settings.ollama_base_url,
                    temperature=0.6,
                    request_timeout=30,
                    num_predict=512,
                    keep_alive=-1,   # mantener el modelo cargado en VRAM
                    think=False,     # qwen3: sin modo thinking → respuesta directa
                )
                logger.info(
                    f"[fast_chat] LLM rápido: model={settings.llm_model_fast} "
                    f"keep_alive=-1 think=False"
                )
    return _fast_llm


async def handle_fast_chat(
    question: str,
    user_id: str,
    request_id: str = "",
    conversation_id: str = "",
) -> dict:
    """
    Resuelve un mensaje de charla simple con una sola llamada al modelo chico.
    Retorna el mismo contrato que el dispatcher (final_answer, dispatch_mode, ...).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    t0 = time.monotonic()
    llm = _get_fast_llm()
    messages = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=question)]

    try:
        resp = await asyncio.to_thread(llm.invoke, messages)
        answer = resp.content if hasattr(resp, "content") else str(resp)
        answer = (answer or "").strip()
    except Exception as exc:
        logger.warning(f"[fast_chat] fallo del modelo rápido, se delega al pipeline: {exc}")
        raise  # el dispatcher hace fallback al pipeline

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        f"[fast_chat] respuesta en {elapsed_ms}ms",
        extra={"request_id": request_id, "user_id": user_id,
               "conversation_id": conversation_id},
    )
    try:
        from observability.metrics import PIPELINE_E2E_LATENCY_SECONDS
        PIPELINE_E2E_LATENCY_SECONDS.labels(intent="chat").observe((time.monotonic() - t0))
    except Exception:
        pass

    return {
        "final_answer":  answer,
        "intent":        "chat",
        "dispatch_mode": "fast_chat",
        "request_id":    request_id,
        "success":       True,
    }


# Token exacto que el modelo chico devuelve cuando la pregunta requiere el pipeline.
_ESCALATE_TOKEN = "NECESITA_PIPELINE"

_TRIAGE_SYSTEM = (
    "Eres Amael, asistente personal en español de México.\n"
    "Tu tarea es decidir cómo responder al mensaje del usuario:\n"
    "- Si es charla, o una pregunta de CONOCIMIENTO GENERAL que puedes responder "
    "tú solo (definiciones, explicaciones de conceptos, traducciones, matemáticas "
    "simples, redacción), respóndela de forma breve, correcta y natural.\n"
    f"- Si para responder necesitas datos del usuario, de su cluster de Kubernetes, "
    "su calendario, sus correos, sus documentos indexados, búsqueda web en tiempo "
    f"real, o ejecutar una herramienta, responde EXACTAMENTE con esta única palabra: {_ESCALATE_TOKEN}\n"
    "Nunca inventes datos que no tengas. Ante la duda entre responder o escalar, escala."
)


async def handle_fast_triage(
    question: str,
    user_id: str,
    request_id: str = "",
    conversation_id: str = "",
) -> dict | None:
    """
    Triage con el modelo chico para intents 'general': si es conocimiento general
    lo responde (1 llamada, ~rápido); si necesita datos/herramientas devuelve None
    para que el dispatcher escale al pipeline con qwen3:14b.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    t0 = time.monotonic()
    llm = _get_fast_llm()
    messages = [SystemMessage(content=_TRIAGE_SYSTEM), HumanMessage(content=question)]

    resp = await asyncio.to_thread(llm.invoke, messages)  # excepción → el dispatcher escala
    answer = (resp.content if hasattr(resp, "content") else str(resp) or "").strip()

    # ¿El modelo pidió escalar? (token en la respuesta corta = decisión de escalar)
    if _ESCALATE_TOKEN in answer.upper():
        logger.info(
            f"[fast_triage] escala a pipeline (14b)",
            extra={"request_id": request_id, "user_id": user_id},
        )
        return None

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        f"[fast_triage] respondida por modelo chico en {elapsed_ms}ms",
        extra={"request_id": request_id, "user_id": user_id,
               "conversation_id": conversation_id},
    )
    try:
        from observability.metrics import PIPELINE_E2E_LATENCY_SECONDS
        PIPELINE_E2E_LATENCY_SECONDS.labels(intent="chat").observe((time.monotonic() - t0))
    except Exception:
        pass

    return {
        "final_answer":  answer,
        "intent":        "general",
        "dispatch_mode": "fast_triage",
        "request_id":    request_id,
        "success":       True,
    }
