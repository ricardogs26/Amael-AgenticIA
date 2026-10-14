"""Entrega E: si el CronJob del brief no corrió (nodo abajo a las 7:00, como
el vie 10-oct-2026), el backend lo repone solo. Cada 10 min entre 7:10 y
11:59 en día hábil, si la marca `rate_limit:planner_daily` no existe, se
dispara el brief; la marca la pone el propio brief (una vez al día)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agents.scheduler import runner

TZ = ZoneInfo("America/Mexico_City")


class TestDecision:
    def test_dia_habil_despues_de_7_10_sin_marca_si_repone(self):
        assert runner.brief_catchup_needed(datetime(2026, 10, 14, 7, 20, tzinfo=TZ), key_exists=False)

    def test_con_marca_no(self):
        assert not runner.brief_catchup_needed(datetime(2026, 10, 14, 7, 20, tzinfo=TZ), key_exists=True)

    def test_antes_de_7_10_no_se_adelanta_al_cron(self):
        assert not runner.brief_catchup_needed(datetime(2026, 10, 14, 7, 5, tzinfo=TZ), key_exists=False)

    def test_despues_de_mediodia_ya_no_tiene_sentido(self):
        assert not runner.brief_catchup_needed(datetime(2026, 10, 14, 12, 0, tzinfo=TZ), key_exists=False)

    def test_fin_de_semana_no(self):
        sabado = datetime(2026, 10, 17, 8, 0, tzinfo=TZ)
        assert not runner.brief_catchup_needed(sabado, key_exists=False)


def test_queda_agendado_cada_10_min_en_la_manana_habil(monkeypatch):
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
    trigger, kw = agendados["brief_catchup"]
    assert trigger == "cron" and kw["day_of_week"] == "mon-fri"
    assert kw["hour"] == "7-11" and kw["minute"] == "*/10"
    assert kw["timezone"] == "America/Mexico_City"


async def test_el_job_repone_el_brief_solo_si_falta_la_marca(monkeypatch):
    corridas = []

    class _Redis:
        def __init__(self, existe): self._e = existe
        def exists(self, k): return 1 if self._e else 0
        def set(self, k, v, nx=False, ex=None): return True

    async def _fake_daily():
        corridas.append(1)
        return None
    monkeypatch.setattr("interfaces.api.routers.planner.run_daily_for_all", _fake_daily)
    monkeypatch.setattr(runner, "_ahora_mx", lambda: datetime(2026, 10, 14, 7, 30, tzinfo=TZ))

    monkeypatch.setattr("storage.redis.client.get_redis_client", lambda: _Redis(existe=True))
    await runner._brief_catchup()
    assert corridas == []

    monkeypatch.setattr("storage.redis.client.get_redis_client", lambda: _Redis(existe=False))
    await runner._brief_catchup()
    assert corridas == [1]
