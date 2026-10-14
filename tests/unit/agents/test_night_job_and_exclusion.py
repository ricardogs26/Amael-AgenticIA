"""Entrega A+B cableada: el nocturno se agenda a las 03:45 México con lock, y
«no me interesa X» sobre un pendiente que vino del correo excluye al
remitente para los barridos siguientes."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from agents.scheduler import tasks_storage as ts


class _Redis:
    def __init__(self):
        self.claves = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.claves:
            return None
        self.claves[k] = v
        return True


async def test_el_nocturno_corre_en_una_sola_replica(monkeypatch):
    from agents.scheduler import runner
    corridas = []
    monkeypatch.setattr("agents.scheduler.night_analysis.run_night_analysis_sync",
                        lambda: corridas.append(1) or [])
    monkeypatch.setattr("storage.redis.client.get_redis_client", lambda: _Redis())
    r = _Redis()
    monkeypatch.setattr("storage.redis.client.get_redis_client", lambda: r)
    await runner._night_analysis()
    await runner._night_analysis()
    assert len(corridas) == 1


def test_el_nocturno_queda_agendado_a_las_3_45_mexico(monkeypatch):
    from agents.scheduler import runner
    runner._scheduler = None
    agendados = {}

    class _Sched:
        def __init__(self, timezone=None): pass
        def add_job(self, fn, trigger, id=None, **kw): agendados[id] = (trigger, kw)
        def start(self): pass
        def get_jobs(self): return []
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", _Sched)
    runner.start_scheduler_loop()
    runner._scheduler = None
    trigger, kw = agendados["night_analysis"]
    assert trigger == "cron" and (kw["hour"], kw["minute"]) == (3, 45)
    assert kw["timezone"] == "America/Mexico_City"


def _email_task():
    return ts.Task(
        id=7, user_id="u@x.com", title="Pagar factura Telcel octubre", description="",
        category="personal", priority="media", estimated_minutes=None,
        due_date=date(2026, 10, 20), status="pending", needs_scheduling=False,
        calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 10, 14, tzinfo=UTC), completed_at=None,
        source="email", source_ref="gmail:m1", source_sender="facturacion@telcel.com",
        amount=489.0,
    )


class _Cassiel:
    def __init__(self):
        from agents.scheduler.agent import CassielAgent
        self._apply_task = CassielAgent._apply_task.__get__(self)
        self._set_followup = lambda *a, **k: True
        # Lo que escribió el usuario: la referencia («telcel») tiene que estar
        # en sus palabras para que Cassiel actúe (grounding, 1.18.21).
        self._last_query = "no me interesa lo de telcel"
        self._next_followup_round = 1


def test_no_me_interesa_cancela_y_excluye_al_remitente():
    agente = _Cassiel()
    excluidos = []
    with patch.object(ts, "find_task", return_value=_email_task()), \
         patch.object(ts, "set_status", return_value=True), \
         patch.object(ts, "exclude_sender", side_effect=lambda u, s: excluidos.append((u, s))):
        out = agente._apply_task("task_cancel", {"task_ref": "telcel", "exclude_sender": True},
                                 "u@x.com", "America/Mexico_City")
    assert excluidos == [("u@x.com", "facturacion@telcel.com")]
    assert "telcel.com" in out and "no te vuelvo" in out.lower()


def test_cancelar_sin_pedir_exclusion_no_excluye():
    agente = _Cassiel()
    excluidos = []
    with patch.object(ts, "find_task", return_value=_email_task()), \
         patch.object(ts, "set_status", return_value=True), \
         patch.object(ts, "exclude_sender", side_effect=lambda u, s: excluidos.append(s)):
        agente._apply_task("task_cancel", {"task_ref": "telcel"}, "u@x.com", "America/Mexico_City")
    assert excluidos == []


def test_la_lista_de_pendientes_muestra_monto_y_origen():
    agente = _Cassiel()
    with patch.object(ts, "list_pending", return_value=[_email_task()]):
        out = agente._apply_task("task_list", {}, "u@x.com", "America/Mexico_City")
    assert "$489" in out and "correo" in out
