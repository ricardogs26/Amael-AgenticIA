"""
audio.transcriber — Transcripción de voz a texto con faster-whisper.

Carga el modelo una sola vez (lazy singleton) para evitar overhead por request.
Modelo: 'base' en CPU con quantización int8 — ~147 MB, ~1-3s por mensaje corto.

Uso:
    from audio.transcriber import transcribe_audio_base64
    text = transcribe_audio_base64(base64_str, mimetype="audio/ogg; codecs=opus")
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper import WhisperModel as WhisperModelType

logger = logging.getLogger("audio.transcriber")

# ── Configuración ─────────────────────────────────────────────────────────────
_WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base")
_WHISPER_DEVICE     = "cpu"
_WHISPER_COMPUTE    = "int8"

# ── Singleton lazy del modelo ─────────────────────────────────────────────────
_model: WhisperModelType | None = None


def _get_model() -> WhisperModelType:
    global _model
    if _model is None:
        logger.info(f"[transcriber] Cargando faster-whisper model='{_WHISPER_MODEL_SIZE}' device=cpu…")
        from faster_whisper import WhisperModel
        _model = WhisperModel(
            _WHISPER_MODEL_SIZE,
            device=_WHISPER_DEVICE,
            compute_type=_WHISPER_COMPUTE,
        )
        logger.info("[transcriber] Modelo cargado.")
    return _model


# ── Extensión por mimetype ────────────────────────────────────────────────────
def _ext_for_mimetype(mimetype: str) -> str:
    if "wav" in mimetype:
        return ".wav"
    if "mp3" in mimetype:
        return ".mp3"
    if "mp4" in mimetype:
        return ".mp4"
    if "webm" in mimetype:
        return ".webm"
    return ".ogg"   # default: WhatsApp PTT = audio/ogg; codecs=opus


# ── API pública ───────────────────────────────────────────────────────────────

def transcribe_audio_base64(
    audio_base64: str,
    mimetype: str = "audio/ogg; codecs=opus",
) -> str:
    """
    Transcribe un audio codificado en base64 a texto.

    Args:
        audio_base64: Datos de audio en base64 (sin prefijo data:...).
        mimetype:     MIME type del audio (default: audio/ogg; codecs=opus).

    Returns:
        Texto transcripto, o cadena vacía si falla o no hay voz detectada.
    """
    ext = _ext_for_mimetype(mimetype)

    # Escribir audio a archivo temporal
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(base64.b64decode(audio_base64))
            tmp_path = tmp.name

        model = _get_model()
        segments, info = model.transcribe(
            tmp_path,
            beam_size=5,
            language=None,          # auto-detección de idioma
            vad_filter=True,        # filtra silencios
            vad_parameters={"min_silence_duration_ms": 500},
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        lang = info.language if info else "?"
        logger.info(f"[transcriber] Transcripción OK: lang={lang} texto='{text[:80]}'")
        return text

    except Exception as exc:
        logger.error(f"[transcriber] Error transcribiendo audio: {exc}")
        return ""

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
