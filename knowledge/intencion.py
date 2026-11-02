"""Detecta preguntas sobre el trabajo de Ricardo sin depender del modelo.

Medido el 1-nov-2026: el planner eligió el paso KNOWLEDGE en 2 de 5 preguntas y el router mandó
otra al agente dev por la palabra «code». Por eso esta regla manda y el modelo solo completa.
Sin dependencias: la importan el router y el planner.
"""
from __future__ import annotations

import re

PATRON = (
    r"(?:\b(?:qu[eé]|por\s+qu[eé]|cu[aá]ndo|c[oó]mo)\s+(?:\w+\s+)?"
    r"(?:decidimos|decidi|decidí|acordamos|elegimos|quedamos|resolvimos)\b"
    r"|\b(?:mis\s+notas|qu[eé]\s+notas|notas\s+(?:tengo|sobre|de)\b)"
    r"|^\W*(?:qu[eé]|cu[aá]l|cu[aá]les|c[oó]mo|por\s+qu[eé]|cu[aá]ndo|d[oó]nde|estado|pendientes)\b"
    r".*\b(?:amael|raphael|camael|zaphkiel|cassiel|pixan|kanan|shikigami|consultorioenregla|"
    r"consultorio\s+en\s+regla|rag\s+gobernado|qwen)\b)"
)
_RE = re.compile(PATRON, re.IGNORECASE | re.DOTALL)


def es_consulta_de_conocimiento(pregunta: str) -> bool:
    return _RE.search(pregunta) is not None
