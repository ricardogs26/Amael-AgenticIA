"""Cómo debe fallar el brief diario: diciendo la verdad.

Contexto (14-ago-2026): el brief llegó por WhatsApp como «⚠️ El LLM tardó
demasiado en generar el plan». Medido después con el prompt real: con el
thinking de qwen3 activado el modelo devuelve `content` VACÍO tras 86.8 s, y con
think=False responde JSON válido en 57.6 s. O sea el timeout de 60 s no era la
causa sino el síntoma que tapaba un fallo peor: subirlo habría cambiado el
mensaje de error, no arreglado el brief.
"""
from unittest.mock import MagicMock, patch

import pytest

from agents.productivity import day_planner
from agents.productivity.errors import GoogleAuthRevocada


def test_el_llm_del_planner_no_razona():
    """Razonando, el modelo agota la generación pensando y deja `content`
    vacío. Para una respuesta que DEBE ser JSON, razonar es contraproducente.

    El nombre del campo es parte del contrato: ChatOllama lo llama `reasoning`
    y descarta `think` sin avisar, así que un test que aceptara `think` daría
    verde sobre un flag que no hace nada."""
    day_planner._planner_llm = None
    with patch("agents.base.llm_factory.get_chat_llm") as factory:
        day_planner._get_llm()

    assert factory.call_args.kwargs.get("reasoning") is False
    assert factory.call_args.kwargs.get("fmt") == "json"


def test_el_timeout_del_planner_deja_margen_sobre_lo_medido():
    """57.6 s medidos con think=False contra un timeout de 60 s es una carrera
    que se pierde en cuanto el modelo esté ocupado."""
    day_planner._planner_llm = None
    with patch("agents.base.llm_factory.get_chat_llm") as factory:
        day_planner._get_llm()

    assert factory.call_args.kwargs["timeout"] >= 150


def test_una_respuesta_vacia_no_se_reporta_como_timeout():
    """Distinguirlos importa: el timeout invita a «intenta más tarde» (inútil si
    el modelo siempre devuelve vacío) y esto pide otra cosa."""
    with pytest.raises(ValueError, match="vacía"):
        day_planner._parse_plan(MagicMock(content=""))


async def test_credenciales_revocadas_no_se_reportan_como_dia_libre(monkeypatch):
    monkeypatch.setattr(day_planner, "_perfil_de", lambda _u: "")

    with patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()), \
         patch("agents.productivity.calendar_manager.get_todays_events",
               side_effect=GoogleAuthRevocada(detalle="invalid_grant")), \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar") as sync:
        res = await day_planner.organize_day_for_user("quien@sea.com")

    assert res["error"] == "auth_revocada"
    assert "libre" not in res["summary"].lower()
    assert "autoriza" in res["summary"].lower()
    # Y sobre todo: no escribe nada en un calendario que no pudo leer.
    sync.assert_not_called()
