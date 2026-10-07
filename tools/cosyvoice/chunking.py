"""
Textos largos para CosyVoice (idea 3 «léeme esto», 7-oct-2026). Funciones
puras, sin red: tests en tests/unit/tools/test_cosyvoice_chunking.py.

cosyvoice-service acepta ≤500 caracteres por petición (TTS_MAX_CHARS). Para
leer un correo o un resumen se parte el texto en fragmentos por frase, se
sintetiza cada uno y los WAV se unen en una sola nota de voz.
"""
from __future__ import annotations

import io
import re
import wave

CHUNK_CHARS = 450          # margen bajo el tope de 500 del servicio
MAX_CHARS_GPU = 2500       # ~3 min de audio, ~3 min de síntesis en GPU
MAX_CHARS_CPU = 500        # en CPU (~45 s por segundo de audio) más no cabe

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def max_chars_for(device: str | None) -> int:
    return MAX_CHARS_GPU if device == "cuda" else MAX_CHARS_CPU


def truncate_at_sentence(text: str, limit: int) -> str:
    """Corta en el último fin de frase antes de `limit`; si no hay, en el último
    espacio. Nunca a media palabra (CosyVoice la pronunciaría cortada)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in (". ", "! ", "? ", "… ", "\n"):
        i = head.rfind(sep)
        if i > limit * 0.5:
            return head[: i + 1].strip()
    i = head.rfind(" ")
    return (head[:i] if i > 0 else head).strip()


def split_for_tts(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """Fragmentos ≤ chunk_chars en límites de frase. Una frase más larga que el
    tope se parte en comas y, si aún no cabe, en espacios."""
    out: list[str] = []
    cur = ""

    def push(piece: str) -> None:
        nonlocal cur
        piece = piece.strip()
        if not piece:
            return
        if len(piece) > chunk_chars:
            for sub in _split_long(piece, chunk_chars):
                push(sub)
            return
        cand = f"{cur} {piece}".strip()
        if len(cand) <= chunk_chars:
            cur = cand
        else:
            out.append(cur)
            cur = piece

    for sentence in _SENTENCE_RE.split(text or ""):
        push(sentence)
    if cur:
        out.append(cur)
    return out


def _split_long(piece: str, limit: int) -> list[str]:
    parts: list[str] = []
    for frag in re.split(r"(?<=[,;:])\s+", piece):
        while len(frag) > limit:
            i = frag.rfind(" ", 0, limit)
            i = i if i > 0 else limit
            parts.append(frag[:i])
            frag = frag[i:].strip()
        if frag:
            parts.append(frag)
    return parts


def concat_wavs(wavs: list[bytes], gap_ms: int = 150) -> bytes:
    """Une WAV PCM del mismo formato con una pausa corta entre fragmentos."""
    if not wavs:
        raise ValueError("sin audio que unir")
    params = None
    frames: list[bytes] = []
    for raw in wavs:
        with wave.open(io.BytesIO(raw), "rb") as w:
            p = w.getparams()
            if params is None:
                params = p
            elif (p.nchannels, p.sampwidth, p.framerate) != (params.nchannels, params.sampwidth, params.framerate):
                raise ValueError("los fragmentos tienen formatos distintos")
            if frames:
                silence = int(params.framerate * gap_ms / 1000)
                frames.append(b"\x00" * silence * params.sampwidth * params.nchannels)
            frames.append(w.readframes(w.getnframes()))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        out.writeframes(b"".join(frames))
    return buf.getvalue()
