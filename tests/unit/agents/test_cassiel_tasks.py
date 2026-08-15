"""Tests de Cassiel tareas pendientes (Fase 1). Toda la lógica decidible
se prueba PURA (sin DB): validación, orden, matching, nudges."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

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


class TestValidacion:
    def test_valores_correctos_pasan(self):
        ts.validate_task_fields("personal", "alta", "pending")  # no lanza

    @pytest.mark.parametrize("cat,prio,status", [
        ("trabajo", "alta", "pending"),      # category inventada por el LLM
        ("personal", "urgente", "pending"),  # priority inventada
        ("laboral", "media", "abierta"),     # status inventado
    ])
    def test_valores_inventados_lanzan(self, cat, prio, status):
        with pytest.raises(ValueError):
            ts.validate_task_fields(cat, prio, status)


class TestOrden:
    def test_vencida_gana_a_alta_sin_fecha(self):
        hoy = date(2026, 8, 15)
        vencida_baja = _task(id=1, priority="baja", due_date=date(2026, 8, 10))
        alta_sin_fecha = _task(id=2, priority="alta", due_date=None)
        orden = ts.sorted_pending([alta_sin_fecha, vencida_baja], hoy)
        assert [t.id for t in orden] == [1, 2]

    def test_desempate_por_fecha_mas_proxima(self):
        hoy = date(2026, 8, 15)
        lejana = _task(id=1, priority="media", due_date=date(2026, 8, 30))
        proxima = _task(id=2, priority="media", due_date=date(2026, 8, 20))
        orden = ts.sorted_pending([lejana, proxima], hoy)
        assert [t.id for t in orden] == [2, 1]

    def test_prioridad_ordena_sin_fechas(self):
        hoy = date(2026, 8, 15)
        tareas = [
            _task(id=1, priority="baja"),
            _task(id=2, priority="alta"),
            _task(id=3, priority="media"),
        ]
        assert [t.id for t in ts.sorted_pending(tareas, hoy)] == [2, 3, 1]
