"""
Router /api/conversations — historial de conversaciones del usuario.

Endpoints:
  GET  /api/conversations              — lista las conversaciones del usuario
  POST /api/conversations              — crea una nueva conversación
  GET  /api/conversations/{id}         — obtiene conversación + mensajes
  DELETE /api/conversations/{id}       — elimina conversación y sus mensajes
  GET  /api/conversations/{id}/export  — exporta conversación como JSON o Markdown
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.conversations")

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# ── Modelos ───────────────────────────────────────────────────────────────────

class ConversationSummary(BaseModel):
    id:             str
    title:          str
    message_count:  int
    created_at:     str
    last_active_at: str
    last_message:   str | None = None

class Message(BaseModel):
    id:         str
    role:       str    # "user" | "assistant"
    content:    str
    intent:     str | None = None
    created_at: str

class ConversationDetail(BaseModel):
    id:       str
    title:    str
    messages: list[Message]

class CreateConversationRequest(BaseModel):
    title: str | None = None

class UpdateConversationRequest(BaseModel):
    title: str

class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]

class MessageOut(BaseModel):
    role:    str
    content: str
    ts:      str   # formatted time string, e.g. "14:05"

class MessagesResponse(BaseModel):
    messages: list[MessageOut]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=ConversationListResponse)
def list_conversations(
    user_id: Annotated[str, Depends(get_current_user)],
    limit:   int = 20,
    offset:  int = 0,
    search:  str | None = Query(default=None, max_length=200),
) -> ConversationListResponse:
    """
    Lista las conversaciones del usuario ordenadas por actividad reciente.

    Query params:
        search: texto a buscar en títulos y contenido de mensajes (case-insensitive).
    """
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                if search:
                    # Búsqueda full-text: título de conversación O contenido de mensajes
                    cur.execute(
                        """
                        SELECT DISTINCT
                            c.id,
                            COALESCE(c.title, 'Conversación ' || LEFT(c.id, 8)) AS title,
                            (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) AS message_count,
                            c.created_at::text AS created_at,
                            COALESCE(
                                (SELECT created_at::text FROM messages
                                 WHERE conversation_id = c.id
                                 ORDER BY created_at DESC LIMIT 1),
                                c.created_at::text
                            ) AS last_active_at,
                            (SELECT content FROM messages
                             WHERE conversation_id = c.id
                             ORDER BY created_at DESC LIMIT 1) AS last_message
                        FROM conversations c
                        LEFT JOIN messages m ON m.conversation_id = c.id
                        WHERE c.user_id = %s
                          AND (
                              c.title ILIKE %s
                              OR m.content ILIKE %s
                          )
                        ORDER BY last_active_at DESC
                        LIMIT %s OFFSET %s
                        """,
                        (user_id, f"%{search}%", f"%{search}%", limit, offset),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            c.id,
                            COALESCE(c.title, 'Conversación ' || LEFT(c.id, 8)) AS title,
                            COUNT(m.id)                 AS message_count,
                            c.created_at::text          AS created_at,
                            COALESCE(
                                (SELECT created_at::text FROM messages
                                 WHERE conversation_id = c.id
                                 ORDER BY created_at DESC LIMIT 1),
                                c.created_at::text
                            )                           AS last_active_at,
                            (SELECT content FROM messages
                             WHERE conversation_id = c.id
                             ORDER BY created_at DESC LIMIT 1) AS last_message
                        FROM conversations c
                        LEFT JOIN messages m ON m.conversation_id = c.id
                        WHERE c.user_id = %s
                        GROUP BY c.id, c.title, c.created_at
                        ORDER BY last_active_at DESC
                        LIMIT %s OFFSET %s
                        """,
                        (user_id, limit, offset),
                    )
                rows = cur.fetchall()
        convs = [
            ConversationSummary(
                id=r[0], title=r[1], message_count=r[2],
                created_at=r[3], last_active_at=r[4], last_message=r[5],
            )
            for r in rows
        ]
        return ConversationListResponse(conversations=convs)
    except Exception as exc:
        logger.error(f"[conversations] list error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener conversaciones")


@router.post("", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
def create_conversation(
    body:    CreateConversationRequest,
    user_id: Annotated[str, Depends(get_current_user)],
) -> ConversationSummary:
    """Crea una nueva conversación vacía."""
    conv_id = str(uuid.uuid4())
    title   = body.title or f"Conversación {conv_id[:8]}"
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO conversations (id, user_id, title, created_at) "
                    "VALUES (%s, %s, %s, NOW())",
                    (conv_id, user_id, title),
                )
        now = "just now"
        return ConversationSummary(
            id=conv_id, title=title, message_count=0,
            created_at=now, last_active_at=now,
        )
    except Exception as exc:
        logger.error(f"[conversations] create error: {exc}")
        raise HTTPException(status_code=500, detail="Error al crear conversación")


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    user_id:         Annotated[str, Depends(get_current_user)],
    limit: int = 100,
) -> ConversationDetail:
    """Obtiene una conversación con todos sus mensajes."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Verificar propiedad
                cur.execute(
                    "SELECT id, COALESCE(title, 'Conversación ' || LEFT(id, 8)) "
                    "FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Conversación no encontrada")

                # Mensajes
                cur.execute(
                    """
                    SELECT id, role, content, intent, created_at::text
                    FROM messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (conversation_id, limit),
                )
                msgs = cur.fetchall()

        return ConversationDetail(
            id=row[0],
            title=row[1],
            messages=[
                Message(id=m[0], role=m[1], content=m[2], intent=m[3], created_at=m[4])
                for m in msgs
            ],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[conversations] get error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener conversación")


@router.get("/{conversation_id}/messages", response_model=MessagesResponse)
def get_messages(
    conversation_id: str,
    user_id:         Annotated[str, Depends(get_current_user)],
    limit: int = 100,
) -> MessagesResponse:
    """
    Retorna los mensajes de una conversación en el formato que espera frontend-next.
    Cada mensaje incluye 'ts' como hora formateada (HH:MM).
    """
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Verificar propiedad
                cur.execute(
                    "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Conversación no encontrada")

                cur.execute(
                    """
                    SELECT role, content,
                           to_char(created_at AT TIME ZONE 'America/Mexico_City', 'HH24:MI') AS ts
                    FROM messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (conversation_id, limit),
                )
                rows = cur.fetchall()

        return MessagesResponse(
            messages=[MessageOut(role=r[0], content=r[1], ts=r[2]) for r in rows]
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[conversations] messages error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener mensajes")


@router.patch("/{conversation_id}", response_model=ConversationSummary)
def update_conversation(
    conversation_id: str,
    body:            UpdateConversationRequest,
    user_id:         Annotated[str, Depends(get_current_user)],
) -> ConversationSummary:
    """Actualiza el título de una conversación."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE conversations SET title = %s WHERE id = %s AND user_id = %s "
                    "RETURNING id, title, created_at::text",
                    (body.title, conversation_id, user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Conversación no encontrada")
        return ConversationSummary(
            id=row[0], title=row[1], message_count=0,
            created_at=row[2], last_active_at=row[2],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[conversations] patch error: {exc}")
        raise HTTPException(status_code=500, detail="Error al actualizar conversación")


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: str,
    user_id:         Annotated[str, Depends(get_current_user)],
) -> None:
    """Elimina una conversación y todos sus mensajes."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Verificar propiedad antes de eliminar
                cur.execute(
                    "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Conversación no encontrada")
                cur.execute(
                    "DELETE FROM messages WHERE conversation_id = %s",
                    (conversation_id,),
                )
                cur.execute(
                    "DELETE FROM conversations WHERE id = %s",
                    (conversation_id,),
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[conversations] delete error: {exc}")
        raise HTTPException(status_code=500, detail="Error al eliminar conversación")


@router.get("/{conversation_id}/export")
def export_conversation(
    conversation_id: str,
    user_id:         Annotated[str, Depends(get_current_user)],
    fmt:             str = Query(default="json", pattern="^(json|markdown)$"),
):
    """
    Exporta una conversación completa como JSON o Markdown.

    Query params:
        fmt: "json" (default) | "markdown"

    Returns:
        application/json o text/plain según fmt.
    """
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT title, created_at FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Conversación no encontrada")
                title, created_at = row[0], row[1]

                cur.execute(
                    """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE conversation_id = %s
                    ORDER BY created_at ASC
                    """,
                    (conversation_id,),
                )
                messages = [
                    {"role": r[0], "content": r[1], "created_at": r[2].isoformat()}
                    for r in cur.fetchall()
                ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[conversations] export error: {exc}")
        raise HTTPException(status_code=500, detail="Error al exportar conversación")

    if fmt == "markdown":
        lines = [
            f"# {title}",
            f"*Exportado el {created_at.isoformat() if hasattr(created_at, 'isoformat') else created_at}*",
            "",
        ]
        for msg in messages:
            role_label = "**Usuario**" if msg["role"] == "user" else "**Asistente**"
            lines.append(f"### {role_label}  _{msg['created_at']}_")
            lines.append("")
            lines.append(msg["content"])
            lines.append("")
        return PlainTextResponse(
            content="\n".join(lines),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{conversation_id}.md"'},
        )

    return {
        "id":         conversation_id,
        "title":      title,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        "messages":   messages,
    }
