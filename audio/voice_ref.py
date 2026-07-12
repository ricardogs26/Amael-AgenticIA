"""
audio.voice_ref — Referencia de voz clonada por número de teléfono.

Storage:
  - MinIO bucket `amael-voice-refs`: {phone}.wav (16kHz mono) + {phone}.txt (transcripción)
  - Redis `voice:ref:{phone}`: cache de la transcripción (sin TTL)

Usado por chat._send_voice_note: si el número tiene referencia, las notas de
voz salen con CosyVoice /tts/clone (voz clonada) en vez de Piper.

Todas las funciones son síncronas (llamar con asyncio.to_thread desde rutas async).
"""
from __future__ import annotations

import base64
import io
import logging
import re

logger = logging.getLogger("audio.voice_ref")

_BUCKET        = "amael-voice-refs"
_REDIS_KEY_FMT = "voice:ref:{phone}"


def _get_minio():
    """Cliente MinIO con init lazy — el lifespan del backend no lo inicializa."""
    from storage.minio import client as mc
    try:
        return mc.get_client()
    except RuntimeError:
        from config.settings import get_settings
        s = get_settings()
        return mc.init_client(
            endpoint=s.minio_endpoint,
            access_key=s.minio_access_key,
            secret_key=s.minio_secret_key,
            secure=s.minio_secure,
        )


def _safe_phone(phone: str) -> str:
    # El bridge puede mandar JID completo ('5219...@c.us' / '...@lid') —
    # quedarse con la parte antes de '@' para que el key sea el número puro.
    phone = phone.split("@", 1)[0]
    return re.sub(r"[^0-9A-Za-z]", "", phone)[:32]


def register_voice_reference(phone: str, wav_b64: str, prompt_text: str) -> None:
    """Guarda WAV 16kHz mono (base64) + transcripción en MinIO, cachea en Redis."""
    from storage.redis.client import get_redis_client

    safe = _safe_phone(phone)
    minio = _get_minio()
    if not minio.bucket_exists(_BUCKET):
        minio.make_bucket(_BUCKET)

    wav_bytes = base64.b64decode(wav_b64)
    minio.put_object(_BUCKET, f"{safe}.wav", io.BytesIO(wav_bytes), len(wav_bytes),
                     content_type="audio/wav")
    txt_bytes = prompt_text.encode()
    minio.put_object(_BUCKET, f"{safe}.txt", io.BytesIO(txt_bytes), len(txt_bytes),
                     content_type="text/plain")

    get_redis_client().set(_REDIS_KEY_FMT.format(phone=safe), prompt_text)
    logger.info(f"[voice_ref] Referencia registrada para {safe} "
                f"({len(wav_bytes)} bytes, prompt {len(prompt_text)} chars)")


def get_voice_reference(phone: str) -> tuple[str, str] | None:
    """
    Retorna (wav_base64, prompt_text) de la voz clonada del número, o None.
    Transcripción desde Redis (rehidrata desde MinIO si falta el key).
    """
    from storage.redis.client import get_redis_client

    safe = _safe_phone(phone)
    try:
        minio = _get_minio()
        resp = minio.get_object(_BUCKET, f"{safe}.wav")
        wav_bytes = resp.read()
        resp.close()
        resp.release_conn()
    except Exception as exc:
        logger.info(f"[voice_ref] Sin referencia para {safe}: {type(exc).__name__}: {exc}")
        return None   # sin referencia registrada

    prompt_text = None
    try:
        raw = get_redis_client().get(_REDIS_KEY_FMT.format(phone=safe))
        if raw:
            prompt_text = raw.decode() if isinstance(raw, bytes) else raw
    except Exception:
        pass
    if not prompt_text:
        try:
            resp = minio.get_object(_BUCKET, f"{safe}.txt")
            prompt_text = resp.read().decode()
            resp.close()
            resp.release_conn()
        except Exception as exc:
            logger.warning(f"[voice_ref] WAV sin transcripción para {safe}: {exc}")
            return None
        try:
            # Cache best-effort: sin Redis la referencia sigue siendo válida
            get_redis_client().set(_REDIS_KEY_FMT.format(phone=safe), prompt_text)
        except Exception:
            pass

    return base64.b64encode(wav_bytes).decode(), prompt_text


def delete_voice_reference(phone: str) -> bool:
    """Elimina la referencia (vuelve a la voz Piper). Retorna True si existía."""
    from storage.redis.client import get_redis_client

    safe = _safe_phone(phone)
    existed = get_voice_reference(safe) is not None
    minio = _get_minio()
    for obj in (f"{safe}.wav", f"{safe}.txt"):
        try:
            minio.remove_object(_BUCKET, obj)
        except Exception:
            pass
    try:
        get_redis_client().delete(_REDIS_KEY_FMT.format(phone=safe))
    except Exception:
        pass
    return existed
