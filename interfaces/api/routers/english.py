"""
Router /api/english — respuestas a los ejercicios del english-coach (8-oct-2026).

  POST /api/english/answer {phone, quoted_text, answer} → {reply}

El bridge lo llama cuando el usuario responde CITANDO el mensaje de la
lección. El id de la lección (EN-AAAA-MM-DD) viene en la cita; la lección con
sus respuestas la dejó el job en Redis (english:lesson:<id>, 8 días). La
calificación es determinista (agents/english/grader.py), sin LLM.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.english")

router = APIRouter(prefix="/api/english", tags=["english"],
                   dependencies=[Depends(require_internal_secret)])


class AnswerIn(BaseModel):
    phone:        str = Field(min_length=6, max_length=64)
    quoted_text:  str = Field(default="", max_length=4000)
    answer:       str = Field(default="", max_length=500)
    # Respuesta por nota de voz (8-oct-2026): OGG/opus en base64 (~10 MB máx).
    audio_base64: str | None = Field(default=None, max_length=14_000_000)
    mimetype:     str = Field(default="audio/ogg; codecs=opus", max_length=80)


# Pista NEUTRAL: no lleva la frase de la lección. Con ella whisper tiende a
# «oír» la respuesta correcta aunque se haya pronunciado mal, y la práctica de
# pronunciación pierde sentido.
_VOICE_PROMPT = "Answers to an English exercise: number one, number two, option A, B or C."


class AnswerOut(BaseModel):
    reply: str


def _load_lesson(lesson_id: str) -> dict | None:
    from storage.redis.client import get_redis_client
    raw = get_redis_client().get(f"english:lesson:{lesson_id}")
    if not raw:
        return None
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


@router.post("/answer", response_model=AnswerOut)
async def english_answer(body: AnswerIn) -> AnswerOut:
    import asyncio

    from agents.english import grader

    lesson_id = grader.lesson_id(body.quoted_text)
    if not lesson_id:
        return AnswerOut(reply="Quote the *English of the day* message so I know which lesson you're answering.")
    try:
        rec = await asyncio.to_thread(_load_lesson, lesson_id)
    except Exception as exc:
        logger.warning(f"[english] Redis no disponible: {exc}")
        return AnswerOut(reply="I can't check your answers right now. Try again in a minute.")
    if not rec:
        return AnswerOut(reply=f"I don't have lesson {lesson_id} anymore (lessons are kept 8 days).")
    heard = None
    answer = body.answer
    if body.audio_base64:
        from audio.transcriber import transcribe_audio_base64
        heard = await asyncio.to_thread(
            transcribe_audio_base64, body.audio_base64, body.mimetype, "en", _VOICE_PROMPT,
        )
        if not heard:
            return AnswerOut(reply="🎧 I couldn't hear anything in that voice note. Try again, a bit closer to the mic.")
        answer = grader.spoken_to_text(heard)
    reply = grader.feedback(grader.parse_reply(answer), rec, heard=heard)
    logger.info(f"[english] {lesson_id} «{body.answer[:60]}» → {reply.splitlines()[0][:60]}")
    return AnswerOut(reply=reply)
