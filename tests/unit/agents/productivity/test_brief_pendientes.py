"""El brief de las 7:00 lleva la sección «Pendientes» (spec Fase 2 §4) en
TODOS sus caminos: día libre, plan generado y token caducado. Los
pendientes no dependen de Google."""
from unittest.mock import MagicMock, patch

from agents.productivity import day_planner
from agents.productivity.errors import GoogleAuthRevocada

SECCION = "📝 *Pendientes*\n🔴 #2 Llamar al plomero — 2 días de atraso"


def _con_pendientes(monkeypatch):
    monkeypatch.setattr(day_planner, "_perfil_de", lambda _u: "")
    monkeypatch.setattr(day_planner, "_seccion_pendientes", lambda _u, _d: SECCION)


async def test_dia_libre_igual_muestra_pendientes(monkeypatch):
    _con_pendientes(monkeypatch)
    with patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()), \
         patch("agents.productivity.calendar_manager.get_todays_events", return_value=[]), \
         patch("agents.productivity.email_manager.get_unread_emails", return_value=[]):
        res = await day_planner.organize_day_for_user("quien@sea.com")
    assert "libre" in res["summary"].lower()
    assert res["summary"].rstrip().endswith("2 días de atraso")


async def test_token_caducado_igual_muestra_pendientes(monkeypatch):
    _con_pendientes(monkeypatch)
    with patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()), \
         patch("agents.productivity.calendar_manager.get_todays_events",
               side_effect=GoogleAuthRevocada(detalle="invalid_grant")):
        res = await day_planner.organize_day_for_user("quien@sea.com")
    assert res["error"] == "auth_revocada" and SECCION in res["summary"]


async def test_plan_generado_anexa_pendientes_al_final(monkeypatch):
    _con_pendientes(monkeypatch)
    plan = MagicMock(content='{"summary": "Día tranquilo.", "tasks": [], '
                             '"priorities": ["Pagar Telcel"], "warnings": []}')
    llm = MagicMock()
    llm.invoke.return_value = plan
    monkeypatch.setattr(day_planner, "_get_llm", lambda: llm)
    with patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()), \
         patch("agents.productivity.calendar_manager.get_todays_events",
               return_value=[{"summary": "Standup", "start": "09:00", "end": "09:15"}]), \
         patch("agents.productivity.email_manager.get_unread_emails", return_value=[]), \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar", return_value=0):
        res = await day_planner.organize_day_for_user("quien@sea.com")
    s = res["summary"]
    assert s.startswith("Día tranquilo.") and "Prioridades" in s
    assert s.index("Prioridades") < s.index("📝 *Pendientes*")


async def test_sin_pendientes_no_hay_seccion(monkeypatch):
    monkeypatch.setattr(day_planner, "_perfil_de", lambda _u: "")
    monkeypatch.setattr(day_planner, "_seccion_pendientes", lambda _u, _d: "")
    with patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()), \
         patch("agents.productivity.calendar_manager.get_todays_events", return_value=[]), \
         patch("agents.productivity.email_manager.get_unread_emails", return_value=[]):
        res = await day_planner.organize_day_for_user("quien@sea.com")
    assert "Pendientes" not in res["summary"]
