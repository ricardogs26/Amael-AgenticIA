"""El brief de las 7:00 lo arma el código por secciones (Fase 2 A+C+D):
Hoy, Próximos días, Vence esta semana, Pendientes, Plan del día, Alertas.
Los pendientes no dependen de Google y van en TODOS los caminos; el LLM
solo aporta el plan de bloques y, si falla, el brief sale sin esa sección."""
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from agents.productivity import day_planner
from agents.productivity.errors import GoogleAuthRevocada
from agents.scheduler import tasks_storage as ts

HOY = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)


def _task(**kw):
    base = dict(
        id=2, user_id="u@x.com", title="Llamar al plomero", description="", category="personal",
        priority="alta", estimated_minutes=None, due_date=date(2026, 10, 12), status="pending",
        needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC), completed_at=None,
    )
    base.update(kw)
    return ts.Task(**base)


def _base(monkeypatch, tareas=None, analisis=""):
    monkeypatch.setattr(day_planner, "_perfil_de", lambda _u: "")
    monkeypatch.setattr(day_planner, "_hoy_para", lambda _u: HOY)
    monkeypatch.setattr(day_planner, "_pendientes_de", lambda _u: tareas if tareas is not None else [_task()])
    monkeypatch.setattr(day_planner, "_analisis_de", lambda _u, _d: analisis)


def _google(events=None, emails=None, upcoming=None, events_exc=None):
    return (
        patch("agents.productivity.vault_credentials.get_user_credentials", return_value=MagicMock()),
        patch("agents.productivity.calendar_manager.get_todays_events",
              side_effect=events_exc, return_value=events or []),
        patch("agents.productivity.email_manager.get_unread_emails", return_value=emails or []),
        patch("agents.productivity.calendar_manager.get_upcoming_events", return_value=upcoming or []),
    )


async def test_sin_eventos_ni_correos_pero_con_pendientes_si_planifica(monkeypatch):
    """Un día sin calendario no es un día libre si hay pendientes: el LLM
    reparte bloques para avanzarlos."""
    _base(monkeypatch)
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content='{"summary": "Avanza el plomero.", "tasks": ['
                                                '{"title": "Llamar al plomero", "start": "09:00", "end": "09:30"}], '
                                                '"priorities": [], "warnings": []}')
    monkeypatch.setattr(day_planner, "_get_llm", lambda: llm)
    g = _google()
    with g[0], g[1], g[2], g[3], \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar", return_value=1):
        res = await day_planner.organize_day_for_user("u@x.com")
    s = res["summary"]
    assert s.startswith("📅 *Hoy*") and "sin eventos" in s.lower()
    assert "📝 *Pendientes*" in s and "2 días de atraso" in s
    assert "⏰ *Vence esta semana*" in s and "🧭" in s and "libre" not in s.lower()


async def test_token_caducado_igual_muestra_pendientes_y_dice_como_reautorizar(monkeypatch):
    _base(monkeypatch)
    g = _google(events_exc=GoogleAuthRevocada(detalle="invalid_grant"))
    with g[0], g[1], g[2], g[3], \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar") as sync:
        res = await day_planner.organize_day_for_user("u@x.com")
    s = res["summary"]
    assert res["error"] == "auth_revocada"
    assert "📝 *Pendientes*" in s and "⚠️ *Alertas*" in s and "autoriza" in s.lower()
    assert "libre" not in s.lower()
    sync.assert_not_called()


async def test_plan_generado_valida_bloques_y_ordena_secciones(monkeypatch):
    _base(monkeypatch, analisis="💡 Empieza por: #2 Llamar al plomero")
    plan = MagicMock(content='{"summary": "Día cargado.", "tasks": ['
                             '{"title": "Bloque consultorio", "start": "09:00", "end": "10:00"},'
                             '{"title": "Bloque Amael", "start": "10:30", "end": "12:00"}], '
                             '"priorities": ["Plomero"], "warnings": ["Comida larga"]}')
    llm = MagicMock()
    llm.invoke.return_value = plan
    monkeypatch.setattr(day_planner, "_get_llm", lambda: llm)
    eventos = [{"summary": "Standup", "start": "2026-10-14T09:00:00-06:00", "end": "2026-10-14T09:30:00-06:00"}]
    proximos = [{"summary": "Cita dental Regina", "start": "2026-10-24T13:30:00-06:00", "end": "2026-10-24T14:30:00-06:00"}]
    g = _google(events=eventos, upcoming=proximos)
    with g[0], g[1], g[2], g[3], \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar", return_value=1) as sync:
        res = await day_planner.organize_day_for_user("u@x.com")
    s = res["summary"]
    # orden fijo
    idx = [s.index(k) for k in ("📅 *Hoy*", "🔜 *Próximos días*", "⏰ *Vence esta semana*",
                                "📝 *Pendientes*", "🧭 *Plan del día*", "⚠️ *Alertas*")]
    assert idx == sorted(idx)
    assert "sáb 24-oct 13:30 · Cita dental Regina" in s
    assert "💡 Empieza por" in s
    # el bloque encimado con el standup NO se escribe al calendario y se avisa
    escritos = sync.call_args.args[1]["tasks"]
    assert [t["title"] for t in escritos] == ["Bloque Amael"]
    assert "Bloque consultorio" in s and "se encima" in s and "Comida larga" in s
    assert res["tasks_created"] == 1


async def test_si_el_llm_falla_el_brief_sale_sin_plan(monkeypatch):
    _base(monkeypatch)
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("ollama caído")
    monkeypatch.setattr(day_planner, "_get_llm", lambda: llm)
    eventos = [{"summary": "Standup", "start": "2026-10-14T09:00:00-06:00", "end": "2026-10-14T09:30:00-06:00"}]
    g = _google(events=eventos)
    with g[0], g[1], g[2], g[3], \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar") as sync:
        res = await day_planner.organize_day_for_user("u@x.com")
    s = res["summary"]
    assert "📅 *Hoy*" in s and "Standup" in s and "📝 *Pendientes*" in s
    assert "🧭" not in s and "⚠️ *Alertas*" in s and "plan" in s.lower()
    sync.assert_not_called()


async def test_el_prompt_del_plan_recibe_los_pendientes(monkeypatch):
    _base(monkeypatch)
    capturado = {}
    llm = MagicMock()

    def _invoke(prompt):
        capturado["p"] = prompt
        return MagicMock(content='{"summary": "ok", "tasks": [], "priorities": [], "warnings": []}')
    llm.invoke.side_effect = _invoke
    monkeypatch.setattr(day_planner, "_get_llm", lambda: llm)
    g = _google(emails=[{"from": "a@b.c", "subject": "x", "snippet": "y"}])
    with g[0], g[1], g[2], g[3], \
         patch("agents.productivity.calendar_manager.sync_plan_to_calendar", return_value=0):
        await day_planner.organize_day_for_user("u@x.com")
    assert "Llamar al plomero" in capturado["p"]


async def test_sin_pendientes_no_hay_seccion(monkeypatch):
    _base(monkeypatch, tareas=[])
    g = _google()
    with g[0], g[1], g[2], g[3]:
        res = await day_planner.organize_day_for_user("u@x.com")
    assert "Pendientes" not in res["summary"] and "Vence" not in res["summary"]
    assert "Día libre" in res["summary"]
