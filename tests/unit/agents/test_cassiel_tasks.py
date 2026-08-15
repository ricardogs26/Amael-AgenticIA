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


class TestMatch:
    def test_id_numerico(self):
        tareas = [_task(id=7, title="comprar café")]
        assert ts.match_tasks(tareas, "7") == tareas

    def test_substring_case_insensitive(self):
        tareas = [_task(id=1, title="Revisar contrato de renta"),
                  _task(id=2, title="Comprar café")]
        assert [t.id for t in ts.match_tasks(tareas, "café")] == [2]

    def test_ambiguedad_devuelve_todos(self):
        tareas = [_task(id=1, title="llamar al banco"),
                  _task(id=2, title="pagar el banco")]
        assert len(ts.match_tasks(tareas, "banco")) == 2

    def test_sin_coincidencia_lista_vacia(self):
        assert ts.match_tasks([_task(id=1, title="x")], "zzz") == []


class TestNudges:
    HOY = date(2026, 8, 15)

    def test_alta_vencida_elegible_cada_dia(self):
        t = _task(priority="alta", due_date=date(2026, 8, 10))
        assert ts.nudge_eligible(t, self.HOY)

    def test_media_cada_3_dias(self):
        t3 = _task(priority="media", due_date=date(2026, 8, 12))  # 3 días
        t2 = _task(priority="media", due_date=date(2026, 8, 13))  # 2 días
        hoy_mismo = _task(priority="media", due_date=self.HOY)
        assert ts.nudge_eligible(t3, self.HOY)
        assert not ts.nudge_eligible(t2, self.HOY)
        assert ts.nudge_eligible(hoy_mismo, self.HOY)   # el día que toca, siempre

    def test_baja_nunca_nudge_salvo_hoy(self):
        vencida = _task(priority="baja", due_date=date(2026, 8, 1))
        hoy = _task(priority="baja", due_date=self.HOY)
        assert not ts.nudge_eligible(vencida, self.HOY)
        assert ts.nudge_eligible(hoy, self.HOY)

    def test_ya_nudgeada_hoy_no_repite(self):
        t = _task(priority="alta", due_date=date(2026, 8, 10),
                  last_nudge_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC))
        assert not ts.nudge_eligible(t, self.HOY)

    def test_sin_fecha_no_nudge(self):
        assert not ts.nudge_eligible(_task(priority="alta", due_date=None), self.HOY)

    def test_cap_gana_lo_prioritario(self):
        tareas = [
            _task(id=1, priority="baja", due_date=self.HOY),
            _task(id=2, priority="alta", due_date=date(2026, 8, 1)),
            _task(id=3, priority="alta", due_date=date(2026, 8, 5)),
            _task(id=4, priority="media", due_date=self.HOY),
        ]
        sel = ts.select_nudges(tareas, self.HOY, cap=3)
        assert [t.id for t in sel] == [2, 3, 4]


class TestApply:
    @pytest.fixture
    def agent(self, monkeypatch):
        from agents.scheduler.agent import CassielAgent
        a = CassielAgent.__new__(CassielAgent)   # sin __init__: solo se usa _apply
        return a

    def test_task_create_valida_y_confirma(self, agent, monkeypatch):
        creado = {}
        def fake_create(user_id, title, **kw):
            creado.update(user_id=user_id, title=title, **kw)
            return _task(id=9, title=title, **{k: v for k, v in kw.items()
                                               if k in ("category", "priority")})
        monkeypatch.setattr(ts, "create_task", fake_create)
        out = agent._apply(
            {"action": "task_create",
             "task": {"title": "comprar café", "category": "personal",
                      "priority": "baja", "estimated_minutes": 15}},
            "u@x.com", "America/Mexico_City",
        )
        assert creado["user_id"] == "u@x.com" and "café" in out

    def test_task_create_categoria_inventada_no_revienta(self, agent, monkeypatch):
        # El LLM inventó "trabajo": create_task lanza ValueError y _apply
        # la convierte en respuesta legible (patrón execute() actual).
        def boom(*a, **k):
            raise ValueError("Categoría inválida: 'trabajo'")
        monkeypatch.setattr(ts, "create_task", boom)
        out = agent._apply(
            {"action": "task_create", "task": {"title": "x", "category": "trabajo"}},
            "u@x.com", "America/Mexico_City",
        )
        assert "inválida" in out.lower() or "categoría" in out.lower()

    def test_task_done_ambiguo_pregunta(self, agent, monkeypatch):
        def ambiguo(user_id, ref):
            raise ValueError("Hay varias pendientes que coinciden con 'banco': …")
        monkeypatch.setattr(ts, "find_task", ambiguo)
        out = agent._apply({"action": "task_done", "task_ref": "banco"},
                           "u@x.com", "America/Mexico_City")
        assert "varias" in out.lower()

    def test_task_list_ordena_y_filtra(self, agent, monkeypatch):
        tareas = [_task(id=1, title="informe mensual", category="laboral",
                        priority="baja"),
                  _task(id=2, title="vitaminas", category="personal",
                        priority="alta")]
        monkeypatch.setattr(ts, "list_pending", lambda u: tareas)
        out = agent._apply({"action": "task_list", "filter": "personal"},
                           "u@x.com", "America/Mexico_City")
        assert "vitaminas" in out and "#2" in out
        assert "informe mensual" not in out and "#1" not in out


class TestNudgeMessage:
    def test_formato_agrupa_por_usuario(self):
        from agents.scheduler.runner import _format_nudge
        hoy = date(2026, 8, 15)
        tareas = [_task(id=1, title="contrato renta", priority="alta",
                        due_date=date(2026, 8, 12)),
                  _task(id=2, title="comprar café", priority="media",
                        due_date=hoy)]
        msg = _format_nudge(tareas, hoy)
        assert "contrato renta" in msg and "3 día" in msg   # días de atraso
        assert "comprar café" in msg and "hoy" in msg


class TestRuteo:
    @pytest.mark.parametrize("frase", [
        "recuérdame comprar café el día de súper",
        "tengo que revisar el contrato de la renta",
        "anota: colgar el cuadro del vision board",
        "/pendientes",
        "/pendientes laboral",
        "¿qué tengo pendiente?",
        "ya compré el café",
        "ya lo hice",
        "cancela la del café",
    ])
    async def test_frases_de_tarea_rutean_a_reminder(self, frase):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == "reminder", f"{frase!r} → {decision.intent!r}"

    @pytest.mark.parametrize("frase,intent", [
        ("recuerda lo que te dije del proyecto", "memory"),
        ("agenda una reunión con Marco el jueves", "productivity"),
        ("necesito el estado del cluster", "kubernetes"),
    ])
    async def test_no_se_come_otros_intents(self, frase, intent):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == intent
