"""Alertas CRITICAL en voz (agents/sre/voice_alerts.py, idea 5)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agents.sre import voice_alerts as va
from agents.sre.models import Anomaly

MX = ZoneInfo("America/Mexico_City")
DIA = datetime(2026, 10, 8, 15, 0, tzinfo=MX)       # 15:00 MX
NOCHE = datetime(2026, 10, 8, 2, 30, tzinfo=MX)     # 02:30 MX (Chaos Mesh / malla WiFi)


class _Redis:
    def __init__(self):
        self.d = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return False
        self.d[k] = v
        return True

    def get(self, k):
        return self.d.get(k)

    def exists(self, k):
        return 1 if k in self.d else 0

    def expire(self, k, s):
        pass

    def incr(self, k):
        self.d[k] = int(self.d.get(k, 0)) + 1
        return self.d[k]


def _crit(resource="minio", issue="DEPLOYMENT_DEGRADED", severity="CRITICAL"):
    return Anomaly(issue_type=issue, severity=severity, namespace="amael-ia",
                   resource_name=resource, resource_type="Deployment", details="0/1",
                   owner_name=resource)


@pytest.fixture
def run():
    """Corre track() en dos momentos: primera vez y `minutos` después."""
    r = _Redis()
    dichos = []

    def _run(anomaly, start=DIA, minutos=11, recently=False):
        kw = dict(redis=r, speak=dichos.append, recently_deployed=lambda n, ns: recently)
        va.track([anomaly], now=start, **kw)
        return va.track([anomaly], now=start + timedelta(minutes=minutos), **kw)

    _run.dichos = dichos
    _run.redis = r
    return _run


def test_critica_persistente_de_dia_se_dice_en_ingles(run):
    assert run(_crit("minio")) != []
    assert run.dichos == [
        "Critical alert from Raphael: minio has no available replicas, for 11 minutes. "
        "Check WhatsApp for details."
    ]


def test_transitoria_no_se_dice(run):
    """Los reinicios del backend en un deploy duran segundos-minutos."""
    assert run(_crit("amael-agentic-deployment"), minutos=4) == []
    assert run.dichos == []


def test_horario_silencioso(run):
    assert run(_crit("minio"), start=NOCHE) == []


def test_nunca_el_bridge(run):
    assert run(_crit("whatsapp-bridge-deployment")) == []


def test_deploy_reciente_no_se_dice(run):
    assert run(_crit("minio"), recently=True) == []


def test_una_vez_por_episodio(run):
    a = _crit("minio")
    run(a)
    kw = dict(redis=run.redis, speak=run.dichos.append, recently_deployed=lambda n, ns: False)
    assert va.track([a], now=DIA + timedelta(minutes=20), **kw) == []
    assert len(run.dichos) == 1


def test_tope_diario(run, monkeypatch):
    monkeypatch.setattr(va, "MAX_PER_DAY", 2)
    for name in ("minio", "qdrant", "postgres"):
        run(_crit(name))
    assert len(run.dichos) == 2


def test_high_no_se_dice(run):
    assert run(_crit("minio", severity="HIGH")) == []


def test_apagado(run, monkeypatch):
    monkeypatch.setattr(va, "ENABLED", False)
    assert run(_crit("minio")) == []


@pytest.mark.parametrize("hora,silencio", [(23, True), (0, True), (6, True), (7, False), (22, False)])
def test_horario_silencioso_cruza_medianoche(hora, silencio):
    assert va.in_quiet_hours(datetime(2026, 10, 8, hora, 0, tzinfo=MX), "23-7") is silencio


def test_nombres_cortos_y_tipos_sin_frase():
    assert va.short_name("frontend-next-deployment-7974dddd85-khjg2") == "frontend-next"
    assert "node is under resource pressure" in va.sentence("NODE_PRESSURE", "lab-home", 12)
    assert "weird thing on qdrant" in va.sentence("WEIRD_THING", "qdrant", 15)
