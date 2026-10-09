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
# 7-oct-2026: «base» convirtió «Hola Amael, ¿cómo estás?» (2 s) en «Háblame el
# contacto muy buenos días» y Amael contestó sobre un contacto. Medido con 4
# notas reales: base 1/4 correctas, small+prompt 4/4, ~6 s por nota en CPU.
_WHISPER_MODEL_SIZE  = os.environ.get("WHISPER_MODEL", "small")
_WHISPER_DEVICE      = "cpu"
_WHISPER_COMPUTE     = "int8"
_WHISPER_CACHE_DIR   = os.environ.get("WHISPER_CACHE_DIR", "/app/whisper-cache")
# Idioma de las notas de voz. "auto" (o vacío) devuelve la autodetección de
# whisper — que en audios cortos es una apuesta: el 12-ago-2026 un «hola, ¿cómo
# estás? buenos días» de 2.2 s salió como alemán con probabilidad 0.48 y se
# transcribió «Hola, como ist das bei uns die Sammeln?». Amael contestó que no
# entendía la pregunta, y el fallo se leía como del agente y no del audio.
#
# 7-oct-2026: con «es» fijo, una nota en inglés se TRADUCÍA al transcribir
# («I'm fine and thank you» → «Bien, y gracias») y Amael contestaba en español.
# Ahora acepta una lista: «es,en» = detección restringida a esos idiomas (gana
# el más probable de la lista; el alemán de agosto ya no puede ganar). Medido
# con 7 notas reales: 7/7 bien. Un solo código = fijo; «auto»/"" = libre.
_WHISPER_LANGUAGE    = os.environ.get("WHISPER_LANGUAGE", "es,en")
# Pista de VOCABULARIO, no de contenido: le enseña el nombre «Amael». Un saludo
# literal como pista («Hola Amael, ¿cómo estás?») también acertaba, pero empuja
# a whisper a escribir ese saludo cuando el audio no se entiende. "" la apaga.
_WHISPER_PROMPT      = os.environ.get(
    "WHISPER_INITIAL_PROMPT", "Mensaje de voz para Amael, el asistente personal de Ricardo."
)
_WHISPER_PROMPT_EN   = os.environ.get(
    "WHISPER_INITIAL_PROMPT_EN", "Voice message for Amael, Ricardo's personal assistant."
)


def _prompt_for(language: str | None) -> str | None:
    """Pista de vocabulario en el idioma de la nota (una pista en español
    empuja a whisper a escribir en español)."""
    if language == "en":
        return _WHISPER_PROMPT_EN or None
    if language == "es":
        return _WHISPER_PROMPT or None
    return None


def _decode(path: str):
    from faster_whisper import decode_audio
    return decode_audio(path)


def _pick_language(model, audio) -> str | None:
    """None = autodetección libre; un código = fijo; lista = el más probable
    de la lista según detect_language (cualquier otro idioma se ignora)."""
    idioma = _WHISPER_LANGUAGE.strip().lower()
    if idioma in ("", "auto"):
        return None
    langs = [x.strip() for x in idioma.split(",") if x.strip()]
    if len(langs) == 1:
        return langs[0]
    try:
        _, _, probs = model.detect_language(audio=audio, vad_filter=True)
        dist = dict(probs)
        best = max(langs, key=lambda code: dist.get(code, 0.0))
        logger.info(
            "[transcriber] Idioma: " + ", ".join(f"{c}={dist.get(c, 0.0):.2f}" for c in langs)
            + f" → {best}"
        )
        return best
    except Exception as exc:
        logger.warning(f"[transcriber] detect_language falló ({exc}); uso {langs[0]}")
        return langs[0]

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
            download_root=_WHISPER_CACHE_DIR,
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
    language: str | None = None,
    prompt: str | None = None,
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
        audio = _decode(tmp_path)      # una sola decodificación para detectar y transcribir
        # language/prompt por llamada: las respuestas del english-coach se
        # transcriben forzadas a inglés y con su propia pista (8-oct-2026).
        language = language or _pick_language(model, audio)
        segments, info = model.transcribe(
            audio,
            beam_size=5,
            language=language,
            vad_filter=True,        # filtra silencios
            vad_parameters={"min_silence_duration_ms": 500},
            initial_prompt=prompt if prompt is not None else _prompt_for(language),
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
