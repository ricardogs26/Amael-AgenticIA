"""
Router /api/chat — endpoints de conversación.

Endpoints:
  POST /api/chat        — respuesta bloqueante JSON
  POST /api/chat/stream — SSE streaming compatible con frontend-next
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from interfaces.api.auth import check_rate_limit, get_current_user
from observability.logging import set_log_context

logger = logging.getLogger("interfaces.api.chat")

router = APIRouter(prefix="/api", tags=["chat"])


# ── Modelos ───────────────────────────────────────────────────────────────────

class HistoryMessage(BaseModel):
    """Un turno previo de la conversación. role: 'user' | 'assistant'."""
    role:    str
    content: str


class ChatRequest(BaseModel):
    # Acepta tanto 'question' (nuevo) como 'prompt' (whatsapp-bridge legacy)
    question:        str | None = Field(default=None, max_length=4000)
    prompt:          str | None = Field(default=None, max_length=4000)
    conversation_id: str | None = None
    # history: turnos previos de la conversación. El whatsapp-bridge SIEMPRE los
    # envía (loadHistory, últimos 10), pero hasta el 5-ago-2026 este modelo no
    # declaraba el campo y Pydantic lo descartaba en silencio — el chat respondía
    # cada mensaje sin memoria del anterior. Ver orchestration/fast_chat.py.
    history:         list[HistoryMessage] = Field(default_factory=list)
    # user_id opcional: usado por whatsapp-bridge para indicar el usuario real
    # cuando el JWT pertenece al bot de servicio (bot-amael@richardx.dev)
    user_id:         str | None = None
    # phone opcional: número WhatsApp original del remitente (enviado por whatsapp-bridge)
    # permite enviar nota de voz aunque canonical_user_id sea un email
    phone:           str | None = None
    # wa_chat_id opcional: JID completo de WhatsApp (`<id>@c.us` o `<id>@lid`).
    # Target de envío fiable — evita inferir el sufijo a partir del número.
    wa_chat_id:      str | None = None
    # audio_base64: nota de voz recibida por WhatsApp — se transcribe antes de procesar
    audio_base64:    str | None = None
    audio_mimetype:  str | None = Field(default="audio/ogg; codecs=opus")
    # image: imagen enviada por WhatsApp — se analiza con modelo de visión
    image:           str | None = None

    @property
    def effective_question(self) -> str:
        return (self.question or self.prompt or "").strip()

class ChatResponse(BaseModel):
    answer:          str
    response:        str            # alias de answer — compatibilidad con whatsapp-bridge
    conversation_id: str
    request_id:      str
    intent:          str
    dispatch_mode:   str
    elapsed_ms:      float


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(
    body:    ChatRequest,
    user_id: Annotated[str, Depends(get_current_user)],
) -> ChatResponse:
    """
    Endpoint principal de chat. Enruta al agente apropiado y retorna la respuesta.

    Requiere:
        Authorization: Bearer <jwt>

    Rate limit: 15 requests / 60s por usuario.
    """
    # Si el caller es el bot de servicio, usar el user_id del body (usuario real)
    _BOT_USER = "bot-amael@richardx.dev"
    if body.user_id and user_id == _BOT_USER:
        from storage.postgres.client import get_connection
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Verificar por email en user_profile
                    cur.execute(
                        "SELECT 1 FROM user_profile WHERE user_id = %s AND status = 'active'",
                        (body.user_id,),
                    )
                    allowed = cur.fetchone() is not None
                    if not allowed:
                        # Verificar por identidad (número WhatsApp)
                        cur.execute(
                            "SELECT 1 FROM user_identities WHERE identity_value = %s",
                            (body.user_id,),
                        )
                        allowed = cur.fetchone() is not None
        except Exception:
            allowed = False
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Usuario no autorizado")
        effective_user = body.user_id
    else:
        effective_user = user_id

    # Rate limit
    check_rate_limit(effective_user)

    # Rol del usuario (para control de acceso por agente)
    from interfaces.api.auth import get_user_role
    user_role = get_user_role(effective_user)

    # Correlación de logs
    request_id      = str(uuid.uuid4())
    conversation_id = body.conversation_id or str(uuid.uuid4())
    set_log_context(
        request_id=request_id,
        user_id=effective_user,
        conversation_id=conversation_id,
    )

    # Transcripción de audio (si viene nota de voz de WhatsApp)
    # was_audio sobrevive al model_copy de abajo: quien habla por voz recibe voz.
    was_audio = bool(body.audio_base64)
    if body.audio_base64:
        try:
            from audio.transcriber import transcribe_audio_base64
            transcript = await asyncio.get_event_loop().run_in_executor(
                None,
                transcribe_audio_base64,
                body.audio_base64,
                body.audio_mimetype or "audio/ogg; codecs=opus",
            )
            if transcript:
                logger.info(f"[chat] Audio transcripto: '{transcript[:80]}'")
                # Sobreescribe el prompt con el texto transcripto
                body = body.model_copy(update={"question": transcript, "prompt": None, "audio_base64": None})
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="No se detectó voz en el audio enviado.",
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"[chat] Error transcribiendo audio: {exc}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al transcribir el audio.")

    # Análisis de imagen con modelo de visión (qwen2.5-vl)
    if body.image:
        import time as _time
        _t0 = _time.time()
        try:
            answer = await _analyze_image(body.image, body.effective_question or "Describe esta imagen detalladamente.")
        except Exception as exc:
            logger.error(f"[chat] Error analizando imagen: {exc}")
            answer = "No pude analizar la imagen. Por favor intenta de nuevo."
        elapsed = round((_time.time() - _t0) * 1000, 1)
        _persist_message(conversation_id, effective_user, body.effective_question or "[imagen]", answer, request_id, "vision")
        asyncio.ensure_future(_store_memory_episode(effective_user, conversation_id, body.effective_question or "[imagen]", answer))
        return ChatResponse(
            answer=answer, response=answer,
            conversation_id=conversation_id, request_id=request_id,
            intent="vision", dispatch_mode="vision", elapsed_ms=elapsed,
        )

    # Input validation
    from security.validator import validate_prompt
    raw = body.effective_question
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Se requiere 'question' o 'prompt'")
    valid, result = validate_prompt(raw)
    if not valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
    question = result

    # paste:pending — texto reenviado largo sin instrucción: se guarda y se
    # pregunta qué hacer, sin pasar por dispatch (misma familia de early-return
    # que el cache hit y el flujo de imagen, arriba).
    if _is_bare_paste(question):
        _set_pending_paste(effective_user, question)
        _persist_message(conversation_id, effective_user, question,
                         _PASTE_PENDING_PROMPT, request_id, "paste")
        asyncio.ensure_future(_store_memory_episode(
            effective_user, conversation_id, question, _PASTE_PENDING_PROMPT
        ))
        return ChatResponse(
            answer=_PASTE_PENDING_PROMPT, response=_PASTE_PENDING_PROMPT,
            conversation_id=conversation_id, request_id=request_id,
            intent="paste", dispatch_mode="paste_pending", elapsed_ms=0.0,
        )

    # P7-004: Response cache — Redis TTL 60s para queries idénticos del mismo usuario
    _cache_key = None
    _cached_answer = _get_cached_response(effective_user, question)
    if _cached_answer is not None:
        logger.debug(f"[chat] cache hit user={effective_user!r}")
        _persist_message(conversation_id, effective_user, question, _cached_answer, request_id, "cached")
        asyncio.ensure_future(_store_memory_episode(effective_user, conversation_id, question, _cached_answer))
        # `intent` y `elapsed_ms` son obligatorios en ChatResponse. Al omitirlos,
        # CADA cache hit reventaba con ValidationError → HTTP 500. Es decir:
        # repetir una pregunta devolvía un 500 en vez de la respuesta cacheada.
        # Detectado el 6-ago-2026 al lanzar la misma consulta 4 veces seguidas.
        return ChatResponse(answer=_cached_answer, response=_cached_answer,
                            conversation_id=conversation_id, request_id=request_id,
                            intent="cache", elapsed_ms=0.0,
                            dispatch_mode="cache")

    # Routing + dispatch
    try:
        from orchestration import dispatch

        question, decision = await _route_with_followup(effective_user, question)
        tools_map = _build_tools_map(effective_user)

        # Enriquecer con memoria (best-effort, no bloquea si falla).
        # Dos capas con reglas distintas (B2 del plan Hermes):
        #   - perfil: hechos destilados, inyectados COMPLETOS siempre — el
        #     coseno no recupera «prefiere respuestas cortas» cuando la
        #     pregunta es sobre Kong. Cacheado en Redis, tope duro en código.
        #   - episodios: lo circunstancial sí se recupera por similitud.
        profile_block = await _run_in_thread_safe(
            _render_profile_block, effective_user
        )
        memory_ctx = await _retrieve_memory_context(effective_user, question)

        partes = []
        if profile_block:
            partes.append(profile_block)
        if memory_ctx:
            partes.append(f"[Contexto de sesiones anteriores]\n{memory_ctx}")
        dispatch_q = (
            "\n\n".join(partes) + f"\n\n[Pregunta actual]\n{question}"
            if partes else question
        )

        result_dict = await dispatch(
            question=dispatch_q,
            user_id=effective_user,   # usar el usuario real, no el JWT del bot
            tools_map=tools_map,
            routing_decision=decision,
            request_id=request_id,
            conversation_id=conversation_id,
            user_role=user_role,
            history=[m.model_dump() for m in (body.history or [])],
        )
    except Exception as exc:
        logger.error(f"[chat] dispatch error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno procesando tu solicitud",
        )

    # Output sanitization
    from security.sanitizer import sanitize_output
    answer = sanitize_output(result_dict.get("final_answer", ""))

    # P7-004: Guardar en cache (solo respuestas sin media embebida)
    if answer and "[MEDIA:" not in answer:
        _set_cached_response(effective_user, question, answer)

    # Persistir en historial
    _persist_message(
        conversation_id=conversation_id,
        user_id=effective_user,
        question=question,
        answer=answer,
        request_id=request_id,
        intent=result_dict.get("intent", "general"),
    )

    # Enviar nota de voz si el usuario lo pidió (fire-and-forget)
    # Preferir el JID completo (wa_chat_id) — el bridge lo usa tal cual sin inferir sufijo.
    # Fallback a body.phone (número crudo) y, por último, al canonical si es de WhatsApp.
    voice_phone = body.wa_chat_id or (
        body.phone if body.phone and _is_whatsapp_user(body.phone) else (
            effective_user if _is_whatsapp_user(effective_user) else None
        )
    )
    # Nota de voz cuando: (a) el mensaje LLEGÓ como audio — se responde en el
    # mismo canal, sin exigir keywords — o (b) el texto la pide explícitamente.
    if (was_audio or _is_voice_request(question)) and voice_phone:
        asyncio.create_task(_send_voice_note(phone=voice_phone, text=answer))

    # Almacenar episodio en memoria Zaphkiel (fire-and-forget)
    asyncio.create_task(_store_memory_episode(
        user_id=effective_user,
        conversation_id=conversation_id,
        user_message=question,
        assistant_reply=answer,
    ))

    logger.info(
        "Chat request completado",
        extra={
            "intent":        result_dict.get("intent"),
            "dispatch_mode": result_dict.get("dispatch_mode"),
            "elapsed_ms":    result_dict.get("elapsed_ms"),
        },
    )

    return ChatResponse(
        answer=answer,
        response=answer,        # alias para whatsapp-bridge
        conversation_id=conversation_id,
        request_id=request_id,
        intent=result_dict.get("intent", "general"),
        dispatch_mode=result_dict.get("dispatch_mode", "pipeline"),
        elapsed_ms=result_dict.get("elapsed_ms", 0.0),
    )


# ── Streaming endpoint ────────────────────────────────────────────────────────

class ChatStreamRequest(BaseModel):
    prompt:          str                  = Field(..., min_length=1, max_length=4000)
    history:         list[HistoryMessage] = Field(default_factory=list)
    conversation_id: str | None          = None
    agent:           str | None          = None  # "amael" | "raphael" | "camael"


def _sse(type_: str, **kwargs) -> str:
    """Formatea un evento SSE como string."""
    return f"data: {json.dumps({'type': type_, **kwargs})}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    body:    ChatStreamRequest,
    user_id: Annotated[str, Depends(get_current_user)],
) -> StreamingResponse:
    """
    SSE streaming endpoint compatible con frontend-next.

    Emite eventos:
      data: {"type": "status", "msg": "..."}
      data: {"type": "token",  "content": "..."}
      data: {"type": "done"}
      data: {"type": "error",  "msg": "..."}
    """
    check_rate_limit(user_id)

    request_id      = str(uuid.uuid4())
    conversation_id = body.conversation_id or str(uuid.uuid4())
    set_log_context(
        request_id=request_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    from interfaces.api.auth import get_user_role
    user_role = get_user_role(user_id)

    from security.validator import validate_prompt
    valid, result = validate_prompt(body.prompt or "")
    if not valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
    question = result

    # Mapa agente_id → (intent, agents, label)
    _FORCED_AGENTS: dict[str, tuple[str, list[str], str]] = {
        "raphael": ("sre",    ["raphael"], "Raphael (SRE)"),
        "camael":  ("devops", ["camael"],  "Camael (DevOps)"),
    }

    async def generate():
        nonlocal question
        try:
            # paste:pending — mismo hook que /chat: texto largo sin
            # instrucción se guarda y se responde directo por el canal SSE,
            # sin pasar por dispatch.
            if _is_bare_paste(question):
                _set_pending_paste(user_id, question)
                yield _sse("token", content=_PASTE_PENDING_PROMPT)
                yield _sse("done")
                _persist_message(conversation_id, user_id, question,
                                 _PASTE_PENDING_PROMPT, request_id, "paste")
                asyncio.ensure_future(_store_memory_episode(
                    user_id, conversation_id, question, _PASTE_PENDING_PROMPT
                ))
                return

            from orchestration import RoutingDecision, dispatch

            tools_map = _build_tools_map(user_id)

            # Si se especificó un agente concreto, forzar routing sin pasar por el LLM
            forced = _FORCED_AGENTS.get(body.agent or "")
            if forced:
                intent, agents, label = forced
                decision = RoutingDecision(
                    intent=intent,
                    agents=agents,
                    confidence=1.0,
                    routing_reason=f"direct_agent_selection:{body.agent}",
                )
                yield _sse("status", msg=f"Conectando con {label}…")
            else:
                yield _sse("status", msg="Analizando tu pregunta…")
                # Mismo hook de followup que /chat: consume la pregunta
                # abierta de Cassiel para que no secuestre otro canal después.
                question, decision = await _route_with_followup(user_id, question)

            yield _sse("status", msg="Procesando respuesta…")

            # Ejecutar dispatch como tarea concurrente para poder emitir
            # keepalive SSE cada 20s — evita que Cloudflare cierre la
            # conexión (~100s timeout) durante tareas largas como Gabriel.
            dispatch_task = asyncio.ensure_future(dispatch(
                question=question,
                user_id=user_id,
                tools_map=tools_map,
                routing_decision=decision,
                request_id=request_id,
                conversation_id=conversation_id,
                user_role=user_role,
            ))
            while True:
                done, _ = await asyncio.wait({dispatch_task}, timeout=20.0)
                if done:
                    break
                yield _sse("status", msg="Procesando respuesta…")

            result_dict = await dispatch_task

            from security.sanitizer import sanitize_output
            answer = sanitize_output(result_dict.get("final_answer", ""))

            # Emitir tokens palabra por palabra
            words = answer.split(" ")
            for i, word in enumerate(words):
                token = word if i == 0 else f" {word}"
                yield _sse("token", content=token)
                await asyncio.sleep(0.012)

            yield _sse("done")

            # Persistir en background (best-effort)
            _persist_message(
                conversation_id=conversation_id,
                user_id=user_id,
                question=question,
                answer=answer,
                request_id=request_id,
                intent=result_dict.get("intent", "general"),
            )

            logger.info(
                "Chat stream completado",
                extra={
                    "intent":        result_dict.get("intent"),
                    "dispatch_mode": result_dict.get("dispatch_mode"),
                    "elapsed_ms":    result_dict.get("elapsed_ms"),
                },
            )

        except Exception as exc:
            logger.error(f"[chat/stream] error: {exc}", exc_info=True)
            yield _sse("error", msg="Error interno procesando tu solicitud")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",    # desactiva buffering nginx para SSE
        },
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _route_with_followup(user_id: str, question: str):
    """
    Routing con continuidad conversacional — compartido por /chat y
    /chat/stream.

    Dos mecanismos de continuidad, en este orden:

    1. paste:pending — si el turno anterior guardó un texto reenviado
       (ver `_is_bare_paste`) y este mensaje es una respuesta corta
       (< 400 chars), se fusionan y el flujo sigue el ruteo NORMAL sobre
       el texto combinado. Si también hay un followup de Cassiel pendiente,
       el paste merge gana este turno y el followup NO se consume — se deja
       intacto en Redis para la próxima respuesta corta, así no se pierde.
    2. cassiel:followup — si el turno anterior dejó una pregunta abierta
       (Redis, GETDEL = un solo uso), este mensaje es la respuesta: va
       directo a Cassiel con la petición previa como contexto, sin pasar
       por el router. Si el usuario cambió de tema, Cassiel responde normal
       o vuelve a preguntar (action=unclear).

    Retorna (question_posiblemente_combinada, RoutingDecision).
    """
    from agents.scheduler.agent import merge_followup, pop_followup
    from orchestration import AgentRouter, RoutingDecision

    # OJO: comprobar la longitud ANTES de hacer pop — GETDEL es incondicional
    # y destructivo (un solo uso). Si un follow-up largo hiciera el pop sin
    # ir a fusionarse, el texto guardado se perdía sin que nadie lo usara.
    if len(question) < 400:
        pending_paste = _pop_pending_paste(user_id)
        if pending_paste:
            merged = _merge_paste(question, pending_paste)
            return merged, await AgentRouter().route(merged)

    prev = pop_followup(user_id)
    if prev:
        return merge_followup(question, prev), RoutingDecision(
            intent="reminder", agents=["scheduler"], confidence=1.0,
            routing_reason="cassiel_followup",
        )
    return question, await AgentRouter().route(question)


# ── paste:pending — texto reenviado sin instrucción ──────────────────────────
#
# Un mensaje reenviado (aviso escolar, correo, etc.) sin petición explícita
# no debe ir al pipeline de dispatch: Amael pregunta qué hacer con él, guarda
# el texto (Redis, mismo patrón que cassiel:followup) y la siguiente
# respuesta corta se fusiona con ese texto antes de rutear.

_PASTE_INSTRUCTION_RE = re.compile(
    r"resume|res[uú]me|recu[eé]rd|anota|apunta|guarda|analiza|explica|"
    r"traduce|agenda|ayuda|puedes|podr[ií]as|qué\b|cómo\b|cuándo\b|¿|"
    r"dime|hazme|necesito|quiero|busca|revisa",
    re.IGNORECASE,
)

# Un "?" de cierre solo cuenta como pregunta si aparece cerca del inicio del
# mensaje — en un aviso largo puede aparecer por azar mucho más adelante
# (una cita, un horario "9:00?") sin que el mensaje completo sea una
# pregunta. "¿" de apertura sí es señal fuerte en cualquier posición de los
# primeros 200 chars (arriba) porque en español marca inequívocamente el
# INICIO de una interrogación.
_PASTE_QUESTION_MARK_WINDOW = 80

_PASTE_PENDING_TTL_S    = 600
_PASTE_PENDING_MAX_CHARS = 4000

_PASTE_PENDING_PROMPT = (
    "Recibí un texto largo 📄 ¿Qué hago con él? Puedo: crear un recordatorio "
    "de la fecha que menciona, anotarlo como pendiente, resumirlo, guardarlo "
    "en tus documentos, o nada."
)


def _has_instruction(text: str) -> bool:
    """True si los primeros 200 chars sugieren una petición explícita
    (verbo de acción o pregunta acentuada/con apertura ¿), en vez de solo
    texto pegado. Deliberadamente NO dispara con "que"/"como" sin acento
    (conectores comunísimos en español: "les recordamos que…", "el evento
    que se realizará…" — un aviso escolar real los usa todo el tiempo) ni
    con "record" suelto ("recordamos", "recordatorio" NO son instrucción;
    "recuerda"/"recuérdame" SÍ). El "?" de cierre solo cuenta si aparece
    cerca del inicio (ver `_PASTE_QUESTION_MARK_WINDOW`)."""
    if _PASTE_INSTRUCTION_RE.search(text[:200]):
        return True
    return "?" in text[:_PASTE_QUESTION_MARK_WINDOW]


def _is_bare_paste(question: str) -> bool:
    """True si `question` es un texto largo (> 400 chars) SIN instrucción —
    probable reenvío (aviso escolar, correo) que requiere preguntar qué
    hacer antes de dispatchar."""
    return len(question) > 400 and not _has_instruction(question)


def _merge_paste(instruction: str, stored: str) -> str:
    """Combina la instrucción corta del usuario con el texto reenviado que
    quedó pendiente en Redis."""
    return f"{instruction}\n\n[Texto reenviado previamente por el usuario]:\n{stored}"


def _set_pending_paste(user_id: str, text: str) -> None:
    """Guarda el texto reenviado (truncado a 4000 chars, TTL 600s) para
    fusionarlo con la siguiente respuesta corta. Best-effort: sin Redis no
    persiste — el usuario tendría que reenviarlo."""
    try:
        from storage.redis.client import get_redis_client
        get_redis_client().setex(
            f"paste:pending:{user_id}", _PASTE_PENDING_TTL_S,
            text[:_PASTE_PENDING_MAX_CHARS],
        )
    except Exception:
        pass


def _pop_pending_paste(user_id: str) -> str | None:
    """Consume (GETDEL — un solo uso) el texto reenviado pendiente.
    Best-effort: sin Redis alcanzable devuelve None, jamás lanza."""
    try:
        from storage.redis.client import get_redis_client
        raw = get_redis_client().getdel(f"paste:pending:{user_id}")
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)
    except Exception:
        return None


def _build_tools_map(user_id: str) -> dict:
    """
    Construye el tools_map completo para el pipeline LangGraph.

    Combina:
      - Herramientas de agente (k8s, rag, productivity, web_search) — callables (str → str)
      - Integraciones externas (prometheus, grafana, etc.) — BaseTool instances
    """
    tools: dict = {}

    # ── k8s: llama al k8s-agent service (FastAPI en k8s-agent-service:8002) ──
    def _k8s(query: str) -> str:
        import httpx

        from config.settings import settings
        from core.circuit_breaker import CircuitBreaker
        from observability.tracing import get_trace_headers
        from storage.redis.client import get_redis_client

        _cb = CircuitBreaker("k8s_agent", get_redis_client())
        if _cb.is_open():
            return "[k8s-agent] Servicio temporalmente no disponible (circuit breaker abierto). Reintenta en unos segundos."
        try:
            resp = httpx.post(
                f"{settings.k8s_agent_url}/api/k8s-agent",
                json={"query": query, "user_email": user_id},
                headers={
                    "Authorization": f"Bearer {settings.internal_api_secret}",
                    **get_trace_headers(),
                },
                timeout=60.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                _cb.record_success()
                return data.get("response") or data.get("answer") or str(data)
            _cb.record_failure()
            return f"[k8s-agent] Error {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            _cb.record_failure()
            return f"[k8s-agent] No disponible: {exc}"

    tools["k8s"] = _k8s

    # ── rag: búsqueda semántica en Qdrant del usuario ─────────────────────────
    def _rag(query: str) -> str:
        from agents.researcher.rag_retriever import retrieve_documents
        return retrieve_documents(user_id, query) or "No se encontraron documentos relevantes."

    tools["rag"] = _rag

    # ── productivity: llama al productivity-service ───────────────────────────
    def _productivity(query: str) -> str:
        import httpx

        from config.settings import settings
        from core.circuit_breaker import CircuitBreaker
        from observability.tracing import get_trace_headers
        from storage.redis.client import get_redis_client

        _cb = CircuitBreaker("productivity_service", get_redis_client())
        if _cb.is_open():
            return "[productivity] Servicio temporalmente no disponible (circuit breaker abierto). Reintenta en unos segundos."
        try:
            resp = httpx.post(
                f"{settings.productivity_service_url}/api/productivity",
                json={"question": query, "user_id": user_id},
                headers={
                    "Authorization": f"Bearer {settings.internal_api_secret}",
                    **get_trace_headers(),
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                _cb.record_success()
                return data.get("response") or data.get("answer") or str(data)
            _cb.record_failure()
            return f"[productivity] Error {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            _cb.record_failure()
            return f"[productivity] No disponible: {exc}"

    tools["productivity"] = _productivity

    # ── web_search: búsqueda web (síncrona, compatible con asyncio loop) ────────
    def _web_search(query: str) -> str:
        try:
            from agents.researcher.web_searcher import web_search
            return web_search(query)
        except Exception as exc:
            return f"[web_search] Error: {exc}"

    tools["web_search"] = _web_search

    # ── document: generación de documentos formales ───────────────────────────
    def _document(query: str) -> str:
        try:
            from agents.researcher.rag_retriever import retrieve_documents
            context = retrieve_documents(user_id, query, k=3)
            return context or f"Documento generado para: {query}"
        except Exception as exc:
            return f"[document] Error: {exc}"

    tools["document"] = _document

    # ── Integraciones externas (prometheus, grafana, etc.) ────────────────────
    try:
        from tools.registry import ToolRegistry
        for name in ToolRegistry.names():
            tools[name] = ToolRegistry.get_or_none(name)
    except Exception:
        pass

    return tools


def _persist_message(
    conversation_id: str,
    user_id: str,
    question: str,
    answer: str,
    request_id: str,
    intent: str,
) -> None:
    """Guarda el par pregunta/respuesta en PostgreSQL. Best-effort."""
    try:
        from storage.postgres.client import get_connection
        # Auto-título: primeras 60 chars de la pregunta, limpiando espacios
        auto_title = " ".join(question.split())[:60]
        if len(question.strip()) > 60:
            auto_title += "…"

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Asegura que existe la conversación; pone título si aún no tiene
                cur.execute(
                    """
                    INSERT INTO conversations (id, user_id, title, created_at, last_active_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET last_active_at = NOW(),
                            title = COALESCE(conversations.title, EXCLUDED.title)
                    """,
                    (conversation_id, user_id, auto_title),
                )
                # Guarda los mensajes
                cur.execute(
                    """
                    INSERT INTO messages
                        (id, conversation_id, role, content, intent, created_at)
                    VALUES
                        (%s, %s, 'user',      %s, %s, NOW()),
                        (%s, %s, 'assistant', %s, %s, NOW())
                    """,
                    (
                        str(uuid.uuid4()), conversation_id, question,  intent,
                        str(uuid.uuid4()), conversation_id, answer,    intent,
                    ),
                )
    except Exception as exc:
        logger.warning(f"[chat] No se pudo persistir mensaje: {exc}")


def _render_profile_block(user_id: str) -> str:
    from agents.memory_agent.profile import render_profile_block
    return render_profile_block(user_id)


async def _run_in_thread_safe(fn, *args) -> str:
    """to_thread con red: la memoria jamás tumba el chat."""
    import asyncio
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as exc:
        logger.debug(f"[chat] perfil no disponible (no crítico): {exc}")
        return ""


async def _retrieve_memory_context(user_id: str, question: str) -> str:
    """
    Recupera recuerdos relevantes de Zaphkiel para enriquecer el contexto del LLM.
    Retorna string vacío si no hay memorias o si el agente no está disponible.
    Best-effort: nunca lanza excepción.
    """
    try:
        from agents.base.agent_registry import AgentRegistry
        from core.agent_base import AgentContext
        # conversation_id y llm son obligatorios en AgentContext. Omitirlos
        # lanzaba TypeError que el `except` de abajo se tragaba en un
        # logger.debug: la memoria llevaba caída en silencio desde siempre.
        # Zaphkiel no usa el LLM (trabaja con embeddings), de ahí llm=None.
        ctx    = AgentContext(
            user_id=user_id, conversation_id="", request_id="memory-retrieve",
            llm=None, via="memory",
        )
        agent  = AgentRegistry.get("zaphkiel", ctx)
        result = await agent.run({"action": "retrieve", "user_id": user_id, "query": question, "k": 4})
        if result.success and result.output:
            return result.output.get("context", "")
    except Exception as exc:
        logger.debug(f"[chat] memoria no disponible (no crítico): {exc}")
    return ""


async def _store_memory_episode(
    user_id: str,
    conversation_id: str,
    user_message: str,
    assistant_reply: str,
) -> None:
    """
    Almacena el episodio en Zaphkiel de forma asíncrona (fire-and-forget).
    Best-effort: nunca lanza excepción ni bloquea la respuesta al usuario.
    """
    try:
        from agents.base.agent_registry import AgentRegistry
        from core.agent_base import AgentContext
        ctx   = AgentContext(
            user_id=user_id, conversation_id=conversation_id or "",
            request_id="memory-store", llm=None, via="memory",
        )
        agent = AgentRegistry.get("zaphkiel", ctx)
        await agent.run({
            "action":          "store",
            "user_id":         user_id,
            "conversation_id": conversation_id,
            "user_message":    user_message,
            "assistant_reply": assistant_reply,
        })
    except Exception as exc:
        logger.debug(f"[chat] memoria store no crítico: {exc}")


# ── Helpers de voz ────────────────────────────────────────────────────────────

# Palabras clave que indican que el usuario quiere una respuesta en audio
_VOICE_KEYWORDS = (
    "nota de voz", "audio", "por audio", "en audio",
    "mándame un audio", "escuchar", "dime en voz",
    "respóndeme en voz", "voice note", "send audio",
)


def _is_voice_request(question: str) -> bool:
    """True si la pregunta contiene una petición explícita de nota de voz."""
    q = question.lower()
    return any(kw in q for kw in _VOICE_KEYWORDS)


def _is_whatsapp_user(user_id: str) -> bool:
    """True si el user_id es un número de teléfono (usuario de WhatsApp)."""
    # Los usuarios de WhatsApp tienen user_id numérico (ej: 521XXXXXXXXXX)
    return user_id.replace("+", "").replace("-", "").isdigit()


_TTS_STRIP_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emojis, pictogramas, símbolos suplementarios
    "⌀-⏿"           # misc técnicos (⏰⌛⏳…)
    "☀-➿"           # símbolos misc y dingbats (☀✅…)
    "⬀-⯿"           # flechas y símbolos misc
    "←-⇿"           # flechas
    "️‍⃣"      # variation selector, ZWJ, keycap
    "*_`#"                    # énfasis/encabezados markdown que el TTS leería
    "]+"
)


def _strip_for_tts(text: str) -> str:
    """Limpia el texto para síntesis: emojis y marcas markdown fuera.
    El texto original se conserva en el mensaje de WhatsApp — esto solo
    afecta lo que la voz pronuncia."""
    clean = _TTS_STRIP_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", clean).strip()


async def _send_voice_note(phone: str, text: str) -> None:
    """
    Sintetiza el texto y lo envía como nota de voz PTT.

    Prioridad:
      0. Voz clonada del usuario (CosyVoice3 /tts/clone) si registró referencia
         en amael-voice-refs. Lenta en CPU (~minutos) pero es fire-and-forget.
      1. Piper (rápido, estable, acento latinoamericano).
      2. CosyVoice voz por defecto (último recurso).
    Fire-and-forget: nunca lanza excepción ni bloquea la respuesta de texto.
    """
    truncated = _strip_for_tts(text)[:500]
    if not truncated:
        return

    # 0. Voz clonada del usuario si hay referencia registrada
    try:
        import asyncio as _aio

        from audio.voice_ref import get_voice_reference
        ref = await _aio.to_thread(get_voice_reference, phone)
        if ref is not None:
            wav_b64, prompt_text = ref
            from tools.cosyvoice.tool import (
                CosyVoiceTool,
                SynthesizeCloneAndSendInput,
            )
            result = await CosyVoiceTool().synthesize_clone_and_send(
                SynthesizeCloneAndSendInput(
                    text=truncated,
                    phone=phone,
                    reference_audio_base64=wav_b64,
                    prompt_text=prompt_text,
                )
            )
            if result.success:
                logger.info(
                    f"[chat] Nota de voz CLONADA enviada a {phone} "
                    f"({result.data.get('duration_seconds', 0):.1f}s)"
                )
                return
            logger.warning(f"[chat] Voz clonada falló, fallback a Piper: {result.error}")
    except Exception as exc:
        logger.warning(f"[chat] Voz clonada excepción, fallback a Piper: {exc}")

    # 1. Piper (rápido, estable, voz latina consistente)
    try:
        from tools.piper.tool import PiperTool
        from tools.piper.tool import SynthesizeAndSendInput as PiperInput
        result = await PiperTool().synthesize_and_send(
            PiperInput(text=truncated, phone=phone)
        )
        if result.success:
            logger.info(f"[chat] Nota de voz Piper enviada a {phone} ({result.data.get('duration_seconds', 0):.1f}s)")
            return
        logger.warning(f"[chat] Piper falló, intentando CosyVoice: {result.error}")
    except Exception as exc:
        logger.warning(f"[chat] Piper excepción, intentando CosyVoice: {exc}")

    # 2. Fallback a CosyVoice
    try:
        from tools.cosyvoice.tool import CosyVoiceTool
        from tools.cosyvoice.tool import SynthesizeAndSendInput as CosyInput
        result = await CosyVoiceTool().synthesize_and_send(
            CosyInput(text=truncated, phone=phone, language="es")
        )
        if result.success:
            logger.info(f"[chat] Nota de voz CosyVoice enviada a {phone} ({result.data.get('duration_seconds', 0):.1f}s)")
        else:
            logger.warning(f"[chat] CosyVoice también falló: {result.error}")
    except Exception as exc:
        logger.debug(f"[chat] _send_voice_note fallback CosyVoice: {exc}")


# ── P7-004: Response cache helpers ────────────────────────────────────────────

_CHAT_CACHE_TTL = 60  # segundos


def _chat_cache_key(user_id: str, question: str) -> str:
    import hashlib
    h = hashlib.sha256(f"{user_id}:{question}".encode()).hexdigest()[:32]
    return f"chat_cache:{h}"


def _get_cached_response(user_id: str, question: str) -> str | None:
    """Retorna respuesta cacheada o None si no hay cache hit."""
    try:
        from storage.redis.client import get_redis_client
        rc = get_redis_client()
        raw = rc.get(_chat_cache_key(user_id, question))
        if raw:
            return raw.decode() if isinstance(raw, bytes) else raw
    except Exception:
        pass
    return None


def _set_cached_response(user_id: str, question: str, answer: str) -> None:
    """Guarda la respuesta en Redis con TTL de 60s."""
    try:
        from storage.redis.client import get_redis_client
        rc = get_redis_client()
        rc.setex(_chat_cache_key(user_id, question), _CHAT_CACHE_TTL, answer)
    except Exception:
        pass


async def _analyze_image(image_base64: str, question: str) -> str:
    """Analiza una imagen usando el modelo de visión vía API nativa de Ollama."""
    from config.settings import settings

    def _call() -> str:
        url = f"{settings.ollama_base_url}/api/chat"
        payload = json.dumps({
            "model": settings.llm_vision_model,
            "messages": [{
                "role": "user",
                "content": question,
                "images": [image_base64],
            }],
            "stream": False,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        # Timeout largo: Ollama necesita descargar qwen2.5:14b y cargar el modelo de visión
        with urllib.request.urlopen(req, timeout=180) as resp:  # nosec B310 — URL es OLLAMA_BASE_URL (variable de entorno interna, no input de usuario)
            data = json.loads(resp.read())
        return data.get("message", {}).get("content", "No pude interpretar la imagen.")

    return await asyncio.to_thread(_call)
