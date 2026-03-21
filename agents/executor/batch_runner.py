"""
Lógica de ejecución paralela de batches del plan.

Migrado desde backend-ia/agents/executor.py.
Responsabilidad única: ejecutar un batch de pasos (paralelo o secuencial).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_ollama import ChatOllama

from agents.executor.step_handlers import STEP_HANDLERS
from observability.metrics import (
    EXECUTOR_BACKPRESSURE_QUEUE_DEPTH,
    EXECUTOR_CONTEXT_TRUNCATIONS_TOTAL,
    EXECUTOR_ERRORS_TOTAL,
    EXECUTOR_ESTIMATED_PROMPT_TOKENS,
    EXECUTOR_PARALLEL_BATCH_SIZE,
    EXECUTOR_PARALLEL_BATCHES_TOTAL,
    EXECUTOR_STEP_LATENCY_SECONDS,
    EXECUTOR_STEPS_TOTAL,
    LLM_TOKENS_TOTAL,
)
from observability.tracing import tracer

logger = logging.getLogger("agents.executor.batch_runner")

MAX_CONTEXT_CHARS = 12_000
MAX_ANSWER_CHARS = 8_000

# Pattern para tags de media embebidos (imágenes Grafana, etc.)
_MEDIA_PATTERN = re.compile(r"\[MEDIA:[^\]]+\]", re.DOTALL)

# Singleton LLM para REASONING — ChatOllama para control de idioma via SystemMessage
_llm_reasoning: ChatOllama | None = None

# Backpressure: límite de llamadas LLM/herramientas concurrentes
# Evita que un burst de requests simultáneos sature Ollama o agote threads
_MAX_CONCURRENT_STEPS = 4
_MAX_QUEUE_DEPTH = 12
_step_semaphore = threading.Semaphore(_MAX_CONCURRENT_STEPS)
_pending_steps = 0
_pending_lock = threading.Lock()


def _track_llm_tokens(response, model: str, input_text: str) -> None:
    """Registra tokens de entrada y salida en LLM_TOKENS_TOTAL."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
        else:
            # Estimación: chars / 4
            input_tokens = len(input_text) // 4
            output_content = getattr(response, "content", "") or ""
            output_tokens = len(output_content) // 4
        LLM_TOKENS_TOTAL.labels(model=model, token_type="input").inc(input_tokens)
        LLM_TOKENS_TOTAL.labels(model=model, token_type="output").inc(output_tokens)
    except Exception:
        pass


_LLM_REASONING_TIMEOUT = 90  # segundos — evita thread starvation si Ollama está lento


def _get_llm_reasoning() -> ChatOllama:
    global _llm_reasoning
    if _llm_reasoning is None:
        from config.settings import settings
        _llm_reasoning = ChatOllama(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
            request_timeout=_LLM_REASONING_TIMEOUT,
        )
    return _llm_reasoning


def _truncate(text: str, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    EXECUTOR_CONTEXT_TRUNCATIONS_TOTAL.inc()
    logger.warning(f"[executor] '{label}' truncado {len(text)} → {max_chars} chars.")
    return (
        "[...contexto anterior truncado para ajustar a la ventana del LLM...]\n"
        + text[-max_chars:]
    )


def _step_type(step: str) -> str:
    return step.split(":")[0].strip().upper()


def _run_tool_step_guarded(
    step: str, state: dict[str, Any], tools_map: dict[str, Any]
) -> str:
    """Wrapper con semáforo de backpressure sobre run_tool_step."""
    global _pending_steps
    with _pending_lock:
        _pending_steps -= 1
        EXECUTOR_BACKPRESSURE_QUEUE_DEPTH.set(_pending_steps)
    with _step_semaphore:
        return run_tool_step(step, state, tools_map)


def run_tool_step(
    step: str, state: dict[str, Any], tools_map: dict[str, Any]
) -> str:
    """
    Ejecuta un único paso de herramienta (no-REASONING).
    Thread-safe: solo lee state, nunca lo escribe.
    """
    stype = _step_type(step)
    query = step[len(stype) + 1:].strip()  # quita "TYPE: "
    t0 = time.time()

    with tracer.start_as_current_span(f"agent.executor.{stype.lower()}") as span:
        span.set_attribute("agent.step_type", stype)
        span.set_attribute("agent.step", step[:200])
        try:
            handler = STEP_HANDLERS.get(stype)
            if handler:
                result = handler(query, state, tools_map)
            else:
                EXECUTOR_ERRORS_TOTAL.labels(step_type=stype).inc()
                result = f"Error: Tipo de paso '{stype}' no reconocido."
        except Exception as exc:
            EXECUTOR_ERRORS_TOTAL.labels(step_type=stype).inc()
            logger.error(f"[executor] Error en {stype}: {exc}", exc_info=True)
            span.record_exception(exc)
            result = f"Error en {stype}: {str(exc)[:200]}"

        elapsed = time.time() - t0
        EXECUTOR_STEP_LATENCY_SECONDS.labels(step_type=stype).observe(elapsed)
        EXECUTOR_STEPS_TOTAL.labels(step_type=stype).inc()
        span.set_attribute("agent.step_latency_seconds", elapsed)

    return result


def _get_user_language_preference(user_id: str) -> str:
    """
    Lookup rápido de idioma preferido: Redis (TTL 5min) → PostgreSQL profile.
    Retorna 'es', 'en', o '' (auto-detect por heurística).
    """
    if not user_id:
        return ""
    try:
        from storage.redis.client import get_redis_client
        rc = get_redis_client()
        key = f"user_lang_pref:{user_id}"
        cached = rc.get(key)
        if cached is not None:
            return cached.decode() if isinstance(cached, bytes) else (cached or "")
    except Exception:
        rc = None
        key = None

    lang = ""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT preferences->>'language' FROM user_profile WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                lang = (row[0] or "") if row else ""
    except Exception:
        pass

    try:
        if rc and key:
            rc.setex(key, 300, lang)
    except Exception:
        pass
    return lang


_ES_MARKERS = {"el", "la", "los", "las", "de", "en", "que", "es", "un", "una",
               "por", "para", "con", "del", "se", "no", "y", "a", "su", "al",
               "lo", "le", "me", "te", "más", "si", "ya", "hay", "como", "pero"}
_EN_MARKERS = {"the", "is", "are", "and", "of", "to", "a", "in", "that", "it",
               "for", "on", "with", "as", "be", "this", "was", "by", "or", "an",
               "at", "from", "which", "have", "were", "they", "their", "about"}


def _detect_language(text: str) -> str:
    """
    Heurística rápida: detecta si el texto es mayormente español ('es') o inglés ('en').
    Compara cuántos marcadores de cada idioma aparecen en las primeras 80 palabras.
    """
    words = set(text.lower().split()[:80])
    es_score = len(words & _ES_MARKERS)
    en_score = len(words & _EN_MARKERS)
    if es_score > en_score:
        return "es"
    if en_score > es_score:
        return "en"
    return "und"  # undetermined


def run_reasoning_step(
    step: str, state: dict[str, Any]
) -> tuple[str, str]:
    """
    Ejecuta un paso REASONING: sintetiza el contexto acumulado con el LLM.
    Retorna (nueva_respuesta, contexto_sin_cambios).
    """
    reasoning_task = step[len("REASONING:"):].strip()
    current_answer = state.get("final_answer", "") or ""
    context = state.get("context", "") or ""

    # Extrae tags de media ANTES de truncar (se restauran al final)
    all_media = _MEDIA_PATTERN.findall(current_answer)
    if all_media:
        current_answer = _MEDIA_PATTERN.sub("[SYSTEM_MEDIA_MARKER]", current_answer)

    context_for_llm = _truncate(current_answer, MAX_ANSWER_CHARS, "final_answer")

    from langchain_core.messages import HumanMessage, SystemMessage

    user_question = state.get("question", "")
    user_id = state.get("user_id", "")

    # Idioma preferido: configuración del perfil > heurística sobre la pregunta
    pref_lang = _get_user_language_preference(user_id)
    if pref_lang == "es":
        lang_rule = (
            "REGLA ABSOLUTA DE IDIOMA: El usuario configuró ESPAÑOL como su idioma preferido. "
            "Responde SIEMPRE en español, sin excepción, aunque el contexto esté en inglés — "
            "traduce y sintetiza al español."
        )
    elif pref_lang == "en":
        lang_rule = (
            "LANGUAGE ABSOLUTE RULE: The user configured ENGLISH as their preferred language. "
            "Always respond in English, without exception."
        )
    else:
        lang_rule = (
            "REGLA ABSOLUTA DE IDIOMA: Responde SIEMPRE en el mismo idioma que usó el usuario "
            "en su pregunta. Si la pregunta está en español, tu respuesta DEBE ser en español, "
            "aunque el contexto o los documentos estén en inglés — en ese caso traduce y sintetiza "
            "el contenido al español. Si la pregunta está en inglés, responde en inglés."
        )

    system_prompt = (
        f"Eres un asistente inteligente. {lang_rule}\n"
        "REGLAS DE FORMATO:\n"
        "1. Si el contexto contiene un bloque ```bash o ```yaml, CÓPIALO EXACTAMENTE sin modificarlo.\n"
        "2. NUNCA pongas análisis ni texto dentro de un bloque ```bash o ```yaml.\n"
        "3. Tu análisis va FUERA de los bloques, como texto normal.\n"
        "4. NO conviertas datos tabulares de kubectl en tablas markdown.\n"
        "5. Si generas scripts bash o manifiestos YAML nuevos, envuélvelos en ```bash o ```yaml.\n"
        "6. SI EL CONTEXTO TIENE [SYSTEM_MEDIA_MARKER], menciona que has analizado la imagen adjunta "
        "pero NO incluyas el marcador en tu respuesta final.\n"
        "7. REGLA ANTI-ALUCINACIONES: Si el contexto NO contiene información técnica específica "
        "sobre el sistema (pods, métricas, estado del cluster), di claramente que no se encontró "
        "información. PERO si la pregunta es de conocimiento general (comparación de modelos, "
        "conceptos técnicos, recomendaciones de arquitectura), usa tu conocimiento de entrenamiento "
        "para responder directamente — NO digas 'no se proporcionó contexto'."
    )

    human_prompt = (
        f"Pregunta del usuario: {user_question}\n\n"
        f"Contexto recuperado:\n{context_for_llm}\n\n"
        f"Tarea: {reasoning_task}"
    )

    estimated_tokens = (len(system_prompt) + len(human_prompt)) // 4
    EXECUTOR_ESTIMATED_PROMPT_TOKENS.labels(step_type="REASONING").observe(estimated_tokens)

    llm = _get_llm_reasoning()
    from config.settings import settings as _settings
    _model = _settings.llm_model
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ])
    _track_llm_tokens(response, _model, system_prompt + human_prompt)
    new_answer = response.content if hasattr(response, "content") else str(response)

    # Post-traducción: si el usuario preguntó en español pero la respuesta salió en inglés,
    # forzar traducción con un prompt dedicado (más confiable que instrucciones en el mismo prompt)
    if user_question and _detect_language(user_question) == "es" and _detect_language(new_answer) == "en":
        logger.info("[executor] Respuesta en inglés detectada para pregunta en español — traduciendo")
        trans_input = new_answer
        trans_response = llm.invoke([
            SystemMessage(content="Eres un traductor experto de inglés a español. Traduce el siguiente texto al español de forma natural y fluida, conservando el formato (bloques de código, listas, etc.) exactamente igual."),
            HumanMessage(content=new_answer),
        ])
        _track_llm_tokens(trans_response, _model, trans_input)
        new_answer = trans_response.content if hasattr(trans_response, "content") else str(trans_response)

    if all_media:
        new_answer += "\n\n" + "\n".join(all_media)

    return new_answer, context


def run_parallel_batch(
    batch: list[str], state: dict[str, Any], tools_map: dict[str, Any]
) -> tuple[str, str]:
    """
    Ejecuta todos los pasos del batch de forma concurrente con ThreadPoolExecutor.
    Solo válido para batches de herramientas (no-REASONING).
    Retorna (respuesta_combinada, contexto_acumulado).
    """
    EXECUTOR_PARALLEL_BATCHES_TOTAL.inc()
    EXECUTOR_PARALLEL_BATCH_SIZE.observe(len(batch))

    # Backpressure: rechazar si hay demasiados pasos pendientes en el sistema
    global _pending_steps
    with _pending_lock:
        if _pending_steps + len(batch) > _MAX_QUEUE_DEPTH:
            logger.warning(
                f"[executor] Backpressure: {_pending_steps} pasos pendientes, "
                f"rechazando batch de {len(batch)}"
            )
            return (
                "Sistema ocupado. Demasiadas tareas concurrentes — intenta de nuevo en unos segundos.",
                state.get("context", "") or "",
            )
        _pending_steps += len(batch)
        EXECUTOR_BACKPRESSURE_QUEUE_DEPTH.set(_pending_steps)

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(len(batch), _MAX_CONCURRENT_STEPS)) as pool:
        future_to_step = {
            pool.submit(_run_tool_step_guarded, step, state, tools_map): step
            for step in batch
        }
        for future in as_completed(future_to_step):
            step = future_to_step[future]
            try:
                results[step] = future.result()
            except Exception as exc:
                stype = _step_type(step)
                EXECUTOR_ERRORS_TOTAL.labels(step_type=stype).inc()
                results[step] = f"Error en {stype}: {str(exc)[:200]}"

    # Combina preservando el orden original del batch
    parts = []
    new_context = state.get("context", "") or ""
    for step in batch:
        stype = _step_type(step)
        result = results.get(step, "")
        parts.append(f"--- {stype} ---\n{result}")
        if stype == "RAG_RETRIEVAL":
            combined = (new_context + "\n" + result).strip() if new_context else result
            new_context = _truncate(combined, MAX_CONTEXT_CHARS, "rag_context")

    return "\n\n".join(parts), new_context
