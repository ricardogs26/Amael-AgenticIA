"""
Tests del modo sombra de HIGH_RESTARTS.

Lo crítico: (1) las reglas deciden con evidencia del pod y escalan cuando falta;
(2) registrar nunca lanza una excepción ni notifica, porque corre dentro del loop;
(3) apagado no toca la base de datos.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.sre import shadow
from agents.sre.models import Anomaly
from core.constants import AnomalyType, Severity


def _evidence(**over):
    data = {"new_restarts": 1, "total_restarts": 6, "last_reason": "Error", "last_exit_code": 1}
    return {**data, **over}


def _anomaly(**meta):
    return Anomaly(
        issue_type=AnomalyType.HIGH_RESTARTS,
        severity=Severity.MEDIUM,
        namespace="amael-ia",
        resource_name="demo-abc",
        resource_type="Pod",
        owner_name="demo",
        details="Pod demo-abc tuvo 1 reinicio(s) nuevo(s).",
        metadata=meta,
    )


@pytest.mark.parametrize(("evidence", "burst", "action", "rule"), [
    (_evidence(), True, shadow.SUPPRESS, "rafaga_cluster"),
    (_evidence(last_reason="OOMKilled"), True, shadow.SUPPRESS, "rafaga_cluster"),
    ({}, False, shadow.ESCALATE, "sin_evidencia"),
    (_evidence(last_reason=None), False, shadow.ESCALATE, "sin_evidencia"),
    (_evidence(last_reason="OOMKilled", new_restarts=5), False, shadow.ESCALATE, "oom"),
    (_evidence(new_restarts=3), False, shadow.ROLLOUT, "reinicios_activos"),
    (_evidence(new_restarts=2), False, shadow.SUPPRESS, "reinicio_aislado"),
    (_evidence(last_reason="Completed", last_exit_code=0), False, shadow.SUPPRESS, "salida_limpia"),
])
def test_reglas(evidence, burst, action, rule):
    assert shadow.decide(evidence, cluster_burst=burst) == (action, rule)


def test_apagado_no_toca_postgres(monkeypatch):
    monkeypatch.setenv("SRE_SHADOW_HIGH_RESTARTS", "false")
    conexion = MagicMock()
    monkeypatch.setattr("storage.postgres.get_connection", conexion)

    assert shadow.record_high_restarts(_anomaly(restart_evidence=_evidence()), "NOTIFY_HUMAN") is False
    conexion.assert_not_called()


def test_otro_tipo_no_se_registra(monkeypatch):
    monkeypatch.setenv("SRE_SHADOW_HIGH_RESTARTS", "true")
    conexion = MagicMock()
    monkeypatch.setattr("storage.postgres.get_connection", conexion)
    anomaly = _anomaly()
    anomaly.issue_type = AnomalyType.CRASH_LOOP

    assert shadow.record_high_restarts(anomaly, "NOTIFY_HUMAN") is False
    conexion.assert_not_called()


def test_falla_de_postgres_no_rompe_el_loop(monkeypatch):
    monkeypatch.setenv("SRE_SHADOW_HIGH_RESTARTS", "true")
    monkeypatch.setattr(
        "storage.postgres.get_connection", MagicMock(side_effect=RuntimeError("sin pool"))
    )

    assert shadow.record_high_restarts(_anomaly(restart_evidence=_evidence()), "NOTIFY_HUMAN") is False


def test_guarda_accion_real_y_decision_sombra(monkeypatch):
    monkeypatch.setenv("SRE_SHADOW_HIGH_RESTARTS", "true")
    cur = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    monkeypatch.setattr("storage.postgres.get_connection", lambda: conn)
    anomaly = _anomaly(restart_evidence=_evidence(new_restarts=4), cluster_burst=False)

    assert shadow.record_high_restarts(anomaly, "NOTIFY_HUMAN") is True

    params = cur.execute.call_args.args[1]
    assert params[0] == anomaly.occurrence_key
    assert "NOTIFY_HUMAN" in params
    assert shadow.ROLLOUT in params
    assert "reinicios_activos" in params


def test_observer_extrae_evidencia_de_la_ultima_terminacion():
    from agents.sre.observer import _restart_evidence

    cs = SimpleNamespace(
        name="app",
        last_state=SimpleNamespace(terminated=SimpleNamespace(reason="OOMKilled", exit_code=137)),
    )

    assert _restart_evidence(cs, new_restarts=2, total_restarts=9) == {
        "container": "app",
        "new_restarts": 2,
        "total_restarts": 9,
        "last_reason": "OOMKilled",
        "last_exit_code": 137,
    }


def test_observer_tolera_pod_sin_terminacion():
    from agents.sre.observer import _restart_evidence

    cs = SimpleNamespace(name="app", last_state=None)

    evidence = _restart_evidence(cs, new_restarts=1, total_restarts=5)

    assert evidence["last_reason"] is None
    assert evidence["last_exit_code"] is None
