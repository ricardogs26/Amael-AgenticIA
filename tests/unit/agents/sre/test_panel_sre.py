"""Panel /admin/agents: learning/stats y slo/status (raphael 1.1.51, 3-nov-2026)."""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

for _k, _v in {"INTERNAL_API_SECRET": "test-secret-" + "x" * 20, "JWT_SECRET_KEY": "x" * 32,
               "SESSION_SECRET_KEY": "x" * 32, "POSTGRES_PASSWORD": "test-pw",
               "MINIO_ACCESS_KEY": "test-ak", "MINIO_SECRET_KEY": "test-sk"}.items():
    os.environ.setdefault(_k, _v)


def _pg_con_filas(filas):
    cur = MagicMock()
    cur.fetchall.return_value = filas
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    modulo = MagicMock(get_connection=get_connection)
    return modulo, cur


def test_learning_stats_agrega_y_calcula_tasa():
    from agents.sre.reporter import get_learning_stats
    modulo, cur = _pg_con_filas([("CRASH_LOOP", "ROLLOUT_RESTART", 4, 3), ("OOM", "PATCH", 2, 0)])
    with patch.dict(sys.modules, {"storage.postgres": modulo}):
        filas = get_learning_stats(days=7)
    assert filas == [
        {"issue_type": "CRASH_LOOP", "action": "ROLLOUT_RESTART", "success_rate": 0.75, "sample_count": 4},
        {"issue_type": "OOM", "action": "PATCH", "success_rate": 0.0, "sample_count": 2},
    ]
    sql, params = cur.execute.call_args[0]
    assert "verify:rollback" in sql and "verify:unresolved" in sql
    assert params == (7, ["NO_ACTION", "NOTIFY_HUMAN"])


def test_learning_stats_acota_dias():
    from agents.sre.reporter import get_learning_stats
    modulo, cur = _pg_con_filas([])
    with patch.dict(sys.modules, {"storage.postgres": modulo}):
        assert get_learning_stats(days=10_000) == []
    assert cur.execute.call_args[0][1][0] == 90


def _slo(status, real, objetivo=0.99, ventana=24):
    return {"handler": "/api/chat", "status": status,
            "target": {"availability": objetivo, "window_hours": ventana},
            "actual": {"availability": real}}


def test_slo_burn_rates_formato_panel():
    from observability import slo
    with patch.object(slo, "get_slo_status", return_value=[_slo("ok", 0.995), _slo("breached", 0.97)]):
        filas = slo.get_slo_burn_rates()
    assert filas[0] == {"handler": "/api/chat", "target_availability": 0.99, "window_hours": 24,
                        "status": "HEALTHY", "current_burn_rate": 0.5}
    assert filas[1]["status"] == "CRITICAL" and filas[1]["current_burn_rate"] == 3.0


def test_slo_sin_datos_omite_burn_rate():
    from observability import slo
    with patch.object(slo, "get_slo_status", return_value=[_slo("no_data", None)]):
        fila = slo.get_slo_burn_rates()[0]
    assert fila["status"] == "NO_DATA" and "current_burn_rate" not in fila


def test_cliente_inprocess_usa_las_funciones_nuevas():
    from clients import raphael_client
    with patch.object(raphael_client, "_is_inprocess", return_value=True), \
         patch("agents.sre.reporter.get_learning_stats", return_value=["L"]) as aprendizaje, \
         patch("observability.slo.get_slo_burn_rates", return_value=["S"]) as s:
        assert raphael_client.get_historical_success_rate(days=3) == ["L"]
        assert raphael_client.get_slo_burn_rates() == ["S"]
    aprendizaje.assert_called_once_with(days=3)
    s.assert_called_once_with()


def test_cliente_remoto_manda_days():
    from clients import raphael_client
    with patch.object(raphael_client, "_is_inprocess", return_value=False), \
         patch.object(raphael_client, "_get", return_value=[{"issue_type": "X"}]) as g:
        assert raphael_client.get_historical_success_rate(days=5) == [{"issue_type": "X"}]
    g.assert_called_once_with("/api/sre/learning/stats", days=5)
