"""Tests de Cassiel tareas pendientes (Fase 1). Toda la lógica decidible
se prueba PURA (sin DB): validación, orden, matching, nudges."""
from __future__ import annotations

from datetime import UTC, datetime

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
