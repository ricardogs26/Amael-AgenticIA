"""
Idioma de la conversación: español o inglés (los dos que usa la plataforma).

7-oct-2026: Ricardo habló en inglés por nota de voz y Amael contestó en
español. Tres cosas lo forzaban: whisper con idioma fijo «es» (traducía el
audio al transcribir), el prompt de la ruta rápida («asistente en español de
México») y la post-traducción del pipeline, que traducía al español cualquier
respuesta si la preferencia guardada era «es» aunque la pregunta fuera en
inglés. Regla nueva, en código: manda el idioma de la PREGUNTA; la preferencia
solo decide cuando no se puede detectar.
"""
from __future__ import annotations

import re

LANGS = ("es", "en")

_ES_MARKERS = {
    "el", "la", "los", "las", "de", "en", "que", "es", "un", "una", "por", "para",
    "con", "del", "se", "no", "y", "su", "al", "lo", "le", "me", "te", "más", "si",
    "ya", "hay", "como", "cómo", "pero", "hola", "qué", "estás", "está", "gracias",
    "buenos", "buenas", "días", "tengo", "tienes", "puedes", "quiero", "dime", "sí",
}
_EN_MARKERS = {
    "the", "is", "are", "and", "of", "to", "in", "that", "it", "for", "on", "with",
    "as", "be", "this", "was", "by", "or", "an", "at", "from", "which", "have",
    "were", "they", "their", "about", "hi", "hello", "how", "you", "what", "can",
    "i", "i'm", "my", "your", "today", "thanks", "thank", "please", "do", "does",
    "speak", "english", "question", "fine", "good", "morning", "where", "when", "why",
}
_WORD_RE = re.compile(r"[a-záéíóúüñ']+")
_QUESTION_HEADER = "[Pregunta actual]\n"


def current_question(text: str) -> str:
    """Lo que va después del último «[Pregunta actual]» que antepone chat.py.
    El contexto de memoria va en español y sesgaría la detección."""
    text = text or ""
    if _QUESTION_HEADER in text:
        return text.rsplit(_QUESTION_HEADER, 1)[-1]
    return text


def detect(text: str) -> str:
    """'es' | 'en' | 'und'. Marcadores en las primeras 80 palabras; ¿ ¡ ñ y
    acentos cuentan como español."""
    t = (text or "").lower()
    words = _WORD_RE.findall(t)[:80]
    es = sum(1 for w in words if w in _ES_MARKERS)
    en = sum(1 for w in words if w in _EN_MARKERS)
    if re.search(r"[¿¡ñáéíóú]", t):
        es += 1
    if es > en:
        return "es"
    if en > es:
        return "en"
    return "und"


def effective(question: str, preference: str | None = None) -> str:
    """Idioma de la respuesta: el de la pregunta; si no se detecta, la
    preferencia del usuario; si no hay, español."""
    lang = detect(current_question(question))
    if lang in LANGS:
        return lang
    return preference if preference in LANGS else "es"


INSTRUCTION = {
    "es": "IDIOMA: responde en español de México.",
    "en": "LANGUAGE: the user wrote in English — respond in English.",
}
