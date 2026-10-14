"""Entrega D: el brief mira 7 días hacia adelante («mañana Dentalitos 13:30»
se avisa el día anterior). La ventana se calcula en la zona del usuario."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from agents.productivity import calendar_manager as cm

TZ = ZoneInfo("America/Mexico_City")
AHORA = datetime(2026, 10, 14, 7, 0, tzinfo=TZ)


def _service(items):
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {"items": items}
    return svc


def test_ventana_de_manana_a_7_dias_en_la_zona_del_usuario(monkeypatch):
    svc = _service([{"summary": "Vuelo", "start": {"dateTime": "2026-10-15T07:10:00-06:00"},
                     "end": {"dateTime": "2026-10-15T09:00:00-06:00"}}])
    monkeypatch.setattr(cm, "_build_calendar_service", lambda creds: svc)

    res = cm.get_upcoming_events(object(), now=AHORA, days=7)

    kw = svc.events.return_value.list.call_args.kwargs
    assert kw["timeMin"].startswith("2026-10-15T00:00:00")
    assert kw["timeMax"].startswith("2026-10-22T00:00:00")
    assert "-06:00" in kw["timeMin"]
    assert kw["singleEvents"] is True and kw["orderBy"] == "startTime"
    assert res[0]["summary"] == "Vuelo" and res[0]["start"].startswith("2026-10-15T07:10")


def test_token_revocado_se_propaga(monkeypatch):
    import pytest

    from agents.productivity.errors import GoogleAuthRevocada
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.side_effect = Exception("invalid_grant")
    monkeypatch.setattr(cm, "_build_calendar_service", lambda creds: svc)
    with pytest.raises(GoogleAuthRevocada):
        cm.get_upcoming_events(object(), now=AHORA)


def test_fallo_pasajero_devuelve_vacio(monkeypatch):
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.side_effect = Exception("503 backend error")
    monkeypatch.setattr(cm, "_build_calendar_service", lambda creds: svc)
    assert cm.get_upcoming_events(object(), now=AHORA) == []


def test_los_eventos_de_hoy_dicen_si_son_del_planner(monkeypatch):
    """Primer brief real con 1.18.19: «📅 Hoy» listó los bloques que el propio
    planner escribió a las 7:00 y `validate_plan_tasks` descartó los nuevos
    por «encimarse» con ellos — que además se borran con `reemplazar`."""
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {"items": [
        {"summary": "Standup", "start": {"dateTime": "2026-10-14T09:00:00-06:00"},
         "end": {"dateTime": "2026-10-14T09:30:00-06:00"}},
        {"summary": "Bloque Amael", "start": {"dateTime": "2026-10-14T10:00:00-06:00"},
         "end": {"dateTime": "2026-10-14T11:00:00-06:00"},
         "extendedProperties": {"private": {"amael_planner": "1"}}},
    ]}
    monkeypatch.setattr(cm, "_build_calendar_service", lambda creds: svc)
    res = cm.get_todays_events(object())
    assert [e["planner"] for e in res] == [False, True]
