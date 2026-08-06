"""
El plan del día se parsea desde la respuesta de un modelo de chat.

`_get_llm()` devuelve un ChatOllama y `.invoke()` de un modelo de chat responde
un `AIMessage`, no una cadena. El código hacía `json.loads(raw)` sobre ese
objeto, así que el brief salía siempre como «❌ No se pudo generar un plan
válido: the JSON object must be str, bytes or bytearray, not AIMessage».
Visto en producción el 6-ago-2026 al disparar el planner a mano.
"""
from __future__ import annotations

import pytest

from agents.productivity.day_planner import _parse_plan


class _AIMessage:
    """Lo mínimo de langchain_core.messages.AIMessage que nos importa."""

    def __init__(self, content):
        self.content = content


PLAN = '{"blocks": [{"start": "09:00", "task": "revisar PRs"}], "priorities": ["deploy"]}'


def test_parsea_un_aimessage():
    plan = _parse_plan(_AIMessage(PLAN))
    assert plan["priorities"] == ["deploy"]
    assert plan["blocks"][0]["task"] == "revisar PRs"


def test_sigue_aceptando_una_cadena():
    """Por si el factory vuelve a un LLM de completado."""
    assert _parse_plan(PLAN)["priorities"] == ["deploy"]


def test_tolera_prosa_y_bloque_markdown():
    """qwen3 tiende a envolver el JSON; el brief no debe morir por eso."""
    envuelto = _AIMessage(f"Claro, aquí va tu plan:\n```json\n{PLAN}\n```\n¡Que rinda el día!")
    assert _parse_plan(envuelto)["priorities"] == ["deploy"]


def test_contenido_en_bloques():
    """Algunos modelos devuelven content como lista de bloques."""
    assert _parse_plan(_AIMessage([{"type": "text", "text": PLAN}]))["priorities"] == ["deploy"]


def test_sin_json_falla_claro():
    with pytest.raises(ValueError, match="sin JSON"):
        _parse_plan(_AIMessage("hoy no tengo nada que decirte"))
