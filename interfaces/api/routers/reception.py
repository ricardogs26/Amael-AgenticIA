"""
Router /api/reception — recepcionista de WhatsApp para números no registrados.

  POST /api/reception/message  — {phone, text, has_media} → {reply | null}
  POST /api/reception/command  — {command, phone}         → {reply}   (/lead, solo admin)

Ambos con el secreto interno (los llama el bridge).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.reception")

router = APIRouter(prefix="/api/reception", tags=["reception"],
                   dependencies=[Depends(require_internal_secret)])


class MessageIn(BaseModel):
    phone:     str = Field(min_length=6, max_length=32)
    text:      str = Field(default="", max_length=4000)
    has_media: bool = False


class ReplyOut(BaseModel):
    reply: str | None


class CommandIn(BaseModel):
    command: str = Field(default="", max_length=2000)
    phone:   str = Field(min_length=6, max_length=32)


@router.post("/message", response_model=ReplyOut)
async def reception_message(body: MessageIn) -> ReplyOut:
    from agents.reception.receptionist import handle_message
    try:
        reply = await asyncio.to_thread(handle_message, body.phone, body.text, body.has_media)
    except Exception as exc:
        logger.error(f"[reception] handle_message reventó: {exc}")
        from agents.reception.prompts import REPLY_ERROR
        reply = REPLY_ERROR
    return ReplyOut(reply=reply)


@router.post("/command", response_model=ReplyOut)
async def reception_command(body: CommandIn) -> ReplyOut:
    from agents.reception.commands import NOT_AVAILABLE, dispatch
    try:
        reply = await asyncio.to_thread(dispatch, body.command, body.phone)
    except Exception as exc:
        logger.error(f"[reception] /lead reventó: {exc}")
        reply = NOT_AVAILABLE
    return ReplyOut(reply=reply)
