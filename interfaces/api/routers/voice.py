"""
Router /api/voice — «léeme esto» (idea 3, 7-oct-2026).

  POST /api/voice/read — {phone, text} → lee el texto TAL CUAL como nota de
  voz (sin LLM), con la voz clonada del usuario si la tiene. Lo usa el bridge
  para `/voz <texto>` y para `/voz` citando un mensaje.

Solo con el secreto interno: el bridge ya verificó que el número está
registrado antes de llegar aquí.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.voice")

router = APIRouter(prefix="/api/voice", tags=["voice"],
                   dependencies=[Depends(require_internal_secret)])


class ReadIn(BaseModel):
    phone: str = Field(min_length=6, max_length=64)   # número o JID (@c.us / @lid)
    text:  str = Field(min_length=1, max_length=20000)


class ReadOut(BaseModel):
    sent:   bool
    engine: str | None = None   # clone | neutral | piper


@router.post("/read", response_model=ReadOut)
async def voice_read(body: ReadIn) -> ReadOut:
    from interfaces.api.routers.chat import _send_voice_note

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Texto vacío")
    engine = await _send_voice_note(phone=body.phone, text=text)
    logger.info(f"[voice] /read {len(text)} chars → {engine or 'sin motor'}")
    return ReadOut(sent=engine is not None, engine=engine)
