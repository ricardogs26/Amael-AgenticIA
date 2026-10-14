"""Pendientes con origen (`source`): los compromisos extraídos del correo se
guardan como tareas de Cassiel con su referencia de Gmail, remitente y monto,
y «no me interesa X» excluye al remitente para siempre."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from agents.scheduler import tasks_storage as ts


def _task(**kw):
    base = dict(
        id=1, user_id="u@x.com", title="t", description="", category="personal",
        priority="media", estimated_minutes=None, due_date=None, status="pending",
        needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 8, 1, tzinfo=UTC), completed_at=None,
    )
    base.update(kw)
    return ts.Task(**base)


@contextmanager
def _conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    yield conn


class TestEsquema:
    def test_task_tiene_origen_con_defaults(self):
        t = _task()
        assert t.source == "chat" and t.source_ref is None
        assert t.source_sender is None and t.amount is None

    def test_el_ddl_agrega_las_columnas_sin_romper_tablas_existentes(self):
        assert "ADD COLUMN IF NOT EXISTS source " in ts._DDL
        assert "ADD COLUMN IF NOT EXISTS source_ref" in ts._DDL
        assert "ADD COLUMN IF NOT EXISTS source_sender" in ts._DDL
        assert "ADD COLUMN IF NOT EXISTS amount" in ts._DDL
        assert "user_task_exclusions" in ts._DDL

    def test_las_columnas_se_leen_en_el_orden_del_dataclass(self):
        # Una columna nueva al final del SELECT y del dataclass: _row_to_task
        # es posicional, así que el orden tiene que coincidir.
        assert ts._COLS.endswith("source, source_ref, source_sender, amount")


class TestDedupYExclusion:
    def test_source_ref_exists_pregunta_sin_importar_el_estado(self):
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        with patch("storage.postgres.client.get_connection", lambda: _conn(cur)):
            assert ts.source_ref_exists("u@x.com", "gmail:m1") is True
        sql = cur.execute.call_args.args[0]
        assert "source_ref" in sql and "status" not in sql   # cancelada también cuenta

    def test_sender_excluded(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        with patch("storage.postgres.client.get_connection", lambda: _conn(cur)):
            assert ts.sender_excluded("u@x.com", "x@telcel.com") is False

    def test_exclude_sender_guarda_en_minusculas_y_es_idempotente(self):
        cur = MagicMock()
        with patch("storage.postgres.client.get_connection", lambda: _conn(cur)):
            ts.exclude_sender("u@x.com", "Facturacion@Telcel.com")
        sql, params = cur.execute.call_args.args
        assert "ON CONFLICT" in sql and params == ("u@x.com", "facturacion@telcel.com")


class TestCreateConOrigen:
    def test_create_task_inserta_origen_remitente_y_monto(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (0,),
            (7, "u@x.com", "Pagar Telcel", "d", "personal", "alta", None, date(2026, 10, 20),
             "pending", False, None, None, datetime(2026, 10, 14, tzinfo=UTC), None,
             "email", "gmail:m1", "facturacion@telcel.com", 489.0),
        ]
        with patch("storage.postgres.client.get_connection", lambda: _conn(cur)):
            t = ts.create_task("u@x.com", "Pagar Telcel", description="d", priority="alta",
                               due_date=date(2026, 10, 20), source="email",
                               source_ref="gmail:m1", source_sender="facturacion@telcel.com",
                               amount=489.0)
        assert t.source == "email" and t.amount == 489.0
        params = cur.execute.call_args.args[1]
        assert params[-4:] == ("email", "gmail:m1", "facturacion@telcel.com", 489.0)
