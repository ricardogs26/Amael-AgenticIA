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
import uuid
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from interfaces.api.auth import check_rate_limit, get_current_user
from observability.logging import set_log_context

logger = logging.getLogger("interfaces.api.chat")

router = APIRouter(prefix="/api", tags=["chat"])


# ── Modelos ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    # Acepta tanto 'question' (nuevo) como 'prompt' (whatsapp-bridge legacy)
    question:        Optional[str] = Field(default=None, max_length=4000)
    prompt:          Optional[str] = Field(default=None, max_length=4000)
    conversation_id: Optional[str] = None
    # user_id opcional: usado por whatsapp-bridge para indicar el usuario real
    # cuando el JWT pertenece al bot de servicio (bot-amael@richardx.dev)
    user_id:         Optional[str] = None

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
    effective_user = body.user_id if (body.user_id and user_id == _BOT_USER) else user_id

    # Rate limit
    check_rate_limit(effective_user)

    # Correlación de logs
    request_id      = str(uuid.uuid4())
    conversation_id = body.conversation_id or str(uuid.uuid4())
    set_log_context(
        request_id=request_id,
        user_id=effective_user,
        conversation_id=conversation_id,
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

    # Routing + dispatch
    try:
        from orchestration import AgentRouter, dispatch
        from tools.registry import ToolRegistry, register_all_tools

        router_inst  = AgentRouter()
        decision     = await router_inst.route(question)
        tools_map    = {name: ToolRegistry.get_or_none(name)
                        for name in ToolRegistry.names()}

        result_dict = await dispatch(
            question=question,
            user_id=effective_user,   # usar el usuario real, no el JWT del bot
            tools_map=tools_map,
            routing_decision=decision,
            request_id=request_id,
            conversation_id=conversation_id,
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

    # Persistir en historial
    _persist_message(
        conversation_id=conversation_id,
        user_id=effective_user,
        question=question,
        answer=answer,
        request_id=request_id,
        intent=result_dict.get("intent", "general"),
    )

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

class HistoryMessage(BaseModel):
    role:    str
    content: str

class ChatStreamRequest(BaseModel):
    prompt:          str                  = Field(..., min_length=1, max_length=4000)
    history:         List[HistoryMessage] = Field(default_factory=list)
    conversation_id: Optional[str]        = None


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

    from security.validator import validate_prompt
    valid, result = validate_prompt(body.prompt or "")
    if not valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
    question = result

    async def generate():
        try:
            yield _sse("status", msg="Analizando tu pregunta…")

            from orchestration import AgentRouter, dispatch
            from tools.registry import ToolRegistry

            router_inst = AgentRouter()
            decision    = await router_inst.route(question)
            tools_map   = {name: ToolRegistry.get_or_none(name)
                           for name in ToolRegistry.names()}

            yield _sse("status", msg="Procesando respuesta…")

            result_dict = await dispatch(
                question=question,
                user_id=user_id,
                tools_map=tools_map,
                routing_decision=decision,
                request_id=request_id,
                conversation_id=conversation_id,
            )

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
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Asegura que existe la conversación
                cur.execute(
                    """
                    INSERT INTO conversations (id, user_id, created_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (conversation_id, user_id),
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
