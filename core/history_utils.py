"""
history_utils — formateo del historial de conversación para prompts.

El pipeline (planner → executor → supervisor) nació sin memoria: cada request se
planificaba y resolvía como si fuera el primer mensaje. Un seguimiento del tipo
"tradúcelo", "y el anterior?" o "hazlo para producción" era imposible de
interpretar porque el LLM nunca veía el turno previo.

`fast_chat` resuelve lo mismo insertando mensajes Human/AI reales en la lista
(ver orchestration/fast_chat.py). Aquí NO se hace así a propósito: el planner
espera devolver un plan en JSON, y meterle turnos de asistente previos como
AIMessage lo induce a continuarlos en vez de planificar. Un bloque de texto
etiquetado es más robusto y sigue el patrón que ya usa chat.py con
"[Contexto de sesiones anteriores]".

Vive en `core/` porque lo consumen tanto `agents/` como `orchestration/`, y es
la única capa que ambos pueden importar sin ciclos.
"""
from __future__ import annotations

from typing import Any

# Coherente con orchestration/fast_chat.py — ver el porqué de los topes allí.
MAX_HISTORY_MSGS = 10
MAX_HISTORY_CHARS = 1200

_ROLE_LABEL = {
    "user": "usuario",
    "assistant": "asistente",
    "ai": "asistente",
    "bot": "asistente",
}


def format_history_block(
    history: list[Any] | None,
    max_msgs: int = MAX_HISTORY_MSGS,
    max_chars: int = MAX_HISTORY_CHARS,
) -> str:
    """
    Convierte el historial en un bloque de texto etiquetado, o "" si no hay nada.

    Acepta dicts {"role","content"} u objetos con esos atributos. Las entradas
    malformadas se omiten: perder un turno degrada la respuesta, pero romper la
    petición deja al usuario sin nada.
    """
    lines: list[str] = []

    for item in (history or [])[-max_msgs:]:
        if isinstance(item, dict):
            role, content = item.get("role"), item.get("content")
        else:
            role, content = getattr(item, "role", None), getattr(item, "content", None)
        if not content:
            continue
        content = str(content).strip().replace("\n", " ")[:max_chars]
        if not content:
            continue
        label = _ROLE_LABEL.get(str(role or "").lower())
        if not label:  # system, tool, roles desconocidos → fuera
            continue
        lines.append(f"{label}: {content}")

    return "\n".join(lines)


def with_history(question: str, history: list[Any] | None) -> str:
    """
    Antepone el historial a la pregunta. Si no hay historial devuelve la
    pregunta tal cual, de modo que el comportamiento previo se conserva byte a
    byte cuando el campo viene vacío.
    """
    block = format_history_block(history)
    if not block:
        return question
    return (
        f"[Conversación previa]\n{block}\n\n"
        f"[Mensaje actual]\n{question}"
    )
