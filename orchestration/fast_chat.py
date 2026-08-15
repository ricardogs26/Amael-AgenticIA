"""
fast_chat — ruta rápida para charla simple (saludos, agradecimientos, respuestas cortas).

En vez de disparar el pipeline completo (planner → executor → supervisor), resuelve el
mensaje con UNA sola llamada al modelo de `LLM_MODEL_FAST` (hoy qwen3.5:9b — el mismo
causal del pipeline, sin orquestador) con keep_alive en VRAM. Latencia ~1-2s vs ~20s+.

El router marca intent="chat" solo para mensajes claramente sociales; cualquier cosa
técnica o analítica sigue yendo al pipeline completo.
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
    "en una frase. No inventes datos ni ejecutes acciones. "
    "La plataforma sintetiza tus respuestas como nota de voz cuando el usuario "
    "escribe o habla por WhatsApp: NUNCA digas que no puedes generar audio ni "
    "menciones limitaciones de audio — responde el contenido y ya. "
    "NO menciones recordatorios, horarios ni hábitos del usuario salvo que él "
    "pregunte por ellos. Si tus respuestas anteriores en el historial repiten "
    "una coletilla o despedida, NO la imites. Usa emojis con mucha moderación "
    "o ninguno."
)


def _get_fast_llm():
    """Singleton del modelo chico. keep_alive=-1 lo mantiene en VRAM; think=False
    evita el modo thinking de qwen3 (que bloquearía la llamada generando <think>)."""
    global _fast_llm
    if _fast_llm is None:
        with _lock:
            if _fast_llm is None:
                from langchain_ollama import ChatOllama

                from config.settings import settings

                _fast_llm = ChatOllama(
                    model=settings.llm_model_fast,
                    base_url=settings.ollama_base_url,
                    temperature=0.6,
                    client_kwargs={"timeout": 30},
                    num_predict=512,
                    keep_alive=-1,   # mantener el modelo cargado en VRAM
                    reasoning=False, # qwen3: sin modo thinking → respuesta directa
                )
                logger.info(
                    f"[fast_chat] LLM rápido: model={settings.llm_model_fast} "
                    f"keep_alive=-1 think=False"
                )
    return _fast_llm


# Turnos de historial que se anteponen al mensaje actual. 10 mensajes (~5
# intercambios) caben de sobra en los 4096 tokens de contexto: el prompt medido
# sin historial es de ~330 tokens.
_MAX_HISTORY_MSGS  = 10
# Corte por mensaje para que un turno largo (p.ej. un volcado de logs pegado por
# el usuario) no se coma la ventana entero.
_MAX_HISTORY_CHARS = 1200


def _build_messages(system_prompt: str, question: str, history: list | None) -> list:
    """
    Arma la lista de mensajes para el LLM: system → historial → pregunta actual.

    `history` viene del cliente como [{"role": "user"|"assistant", "content": str}].
    El bridge de WhatsApp ya lo envía (últimos 10 mensajes), pero hasta el
    5-ago-2026 `ChatRequest` no lo declaraba y Pydantic lo descartaba en
    silencio: la charla se respondía siempre sin memoria del turno anterior.

    Entradas malformadas se ignoran en vez de fallar — perder contexto degrada
    la respuesta, pero romper la petición deja al usuario sin nada.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages: list = [SystemMessage(content=system_prompt)]

    for item in (history or [])[-_MAX_HISTORY_MSGS:]:
        if isinstance(item, dict):
            role, content = item.get("role"), item.get("content")
        else:  # objeto Pydantic u otro con atributos
            role, content = getattr(item, "role", None), getattr(item, "content", None)
        if not content:
            continue
        content = str(content).strip()[:_MAX_HISTORY_CHARS]
        if not content:
            continue
        role = str(role or "").lower()
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role in ("assistant", "ai", "bot"):
            messages.append(AIMessage(content=content))
        # cualquier otro rol (system, tool…) se omite a propósito

    messages.append(HumanMessage(content=question))
    return messages


async def handle_fast_chat(
    question: str,
    user_id: str,
    request_id: str = "",
    conversation_id: str = "",
    history: list | None = None,
) -> dict:
    """
    Resuelve un mensaje de charla simple con una sola llamada al modelo chico.
    Retorna el mismo contrato que el dispatcher (final_answer, dispatch_mode, ...).
    """
    t0 = time.monotonic()
    llm = _get_fast_llm()
    messages = _build_messages(_SYSTEM_PROMPT, question, history)

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
        PIPELINE_E2E_LATENCY_SECONDS.labels(intent="chat").observe(time.monotonic() - t0)
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
    history: list | None = None,
) -> dict | None:
    """
    Triage con el modelo chico para intents 'general': si es conocimiento general
    lo responde (1 llamada, ~rápido); si necesita datos/herramientas devuelve None
    para que el dispatcher escale al pipeline con qwen3:14b.

    El historial importa aquí tanto como en fast_chat: sin él, un "y el
    anterior?" o un "traducelo" se evalúan a ciegas y el triador escala o
    responde mal.
    """
    t0 = time.monotonic()
    llm = _get_fast_llm()
    messages = _build_messages(_TRIAGE_SYSTEM, question, history)

    resp = await asyncio.to_thread(llm.invoke, messages)  # excepción → el dispatcher escala
    answer = (resp.content if hasattr(resp, "content") else str(resp) or "").strip()

    # ¿El modelo pidió escalar? (token en la respuesta corta = decisión de escalar)
    if _ESCALATE_TOKEN in answer.upper():
        logger.info(
            "[fast_triage] escala a pipeline (14b)",
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
        PIPELINE_E2E_LATENCY_SECONDS.labels(intent="chat").observe(time.monotonic() - t0)
    except Exception:
        pass

    return {
        "final_answer":  answer,
        "intent":        "general",
        "dispatch_mode": "fast_triage",
        "request_id":    request_id,
        "success":       True,
    }
