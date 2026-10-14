"""Entrega C: el brief lo arma el CÓDIGO por secciones fijas; el LLM solo
aporta el plan de bloques, y ese plan se valida antes de escribirlo al
calendario (horario laboral, sin encimarse con eventos reales)."""
from __future__ import annotations

from datetime import UTC, date, datetime

from agents.productivity import brief_builder as bb
from agents.scheduler import tasks_storage as ts

HOY = date(2026, 10, 14)
EVENTOS_HOY = [
    {"summary": "Standup", "start": "2026-10-14T09:00:00-06:00", "end": "2026-10-14T09:15:00-06:00"},
    {"summary": "Comida con Lucero", "start": "2026-10-14T14:00:00-06:00", "end": "2026-10-14T15:30:00-06:00"},
    {"summary": "Cumple de mamá", "start": "2026-10-14", "end": "2026-10-15"},   # día completo
]
PROXIMOS = [
    {"summary": "Cita dental Regina", "start": "2026-10-24T13:30:00-06:00", "end": "2026-10-24T14:30:00-06:00"},
    {"summary": "Vuelo MEX-MID", "start": "2026-10-15T07:10:00-06:00", "end": "2026-10-15T09:00:00-06:00"},
]


def _task(**kw):
    base = dict(
        id=1, user_id="u@x.com", title="t", description="", category="personal",
        priority="media", estimated_minutes=None, due_date=None, status="pending",
        needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC), completed_at=None,
    )
    base.update(kw)
    return ts.Task(**base)


class TestValidacionDelPlan:
    def test_bloque_encimado_con_un_evento_se_descarta(self):
        plan = [{"title": "Bloque consultorio", "start": "09:00", "end": "10:30"},
                {"title": "Bloque Amael", "start": "10:30", "end": "12:00"}]
        ok, descartados = bb.validate_plan_tasks(plan, EVENTOS_HOY)
        assert [t["title"] for t in ok] == ["Bloque Amael"]
        assert "Bloque consultorio" in descartados[0]

    def test_fuera_de_horario_laboral_se_descarta(self):
        plan = [{"title": "Madrugada", "start": "06:00", "end": "07:00"},
                {"title": "Noche", "start": "18:30", "end": "19:30"},
                {"title": "Bien", "start": "16:00", "end": "17:30"}]
        ok, _ = bb.validate_plan_tasks(plan, [])
        assert [t["title"] for t in ok] == ["Bien"]

    def test_horas_invalidas_o_fin_antes_del_inicio_se_descartan(self):
        plan = [{"title": "a", "start": "9", "end": "10:00"},
                {"title": "b", "start": "11:00", "end": "10:00"},
                {"title": "c", "start": "25:00", "end": "26:00"}]
        ok, descartados = bb.validate_plan_tasks(plan, [])
        assert ok == [] and len(descartados) == 3

    def test_el_dia_completo_no_bloquea_nada(self):
        plan = [{"title": "Bloque", "start": "10:00", "end": "11:00"}]
        ok, _ = bb.validate_plan_tasks(plan, [EVENTOS_HOY[2]])
        assert len(ok) == 1

    def test_bloques_que_se_enciman_entre_si_conserva_el_primero(self):
        plan = [{"title": "a", "start": "10:00", "end": "11:30"},
                {"title": "b", "start": "11:00", "end": "12:00"}]
        ok, _ = bb.validate_plan_tasks(plan, [])
        assert [t["title"] for t in ok] == ["a"]


class TestSecciones:
    def test_hoy_lista_eventos_con_hora_y_dia_completo(self):
        s = bb.section_today(EVENTOS_HOY)
        assert s.startswith("📅 *Hoy*")
        assert "09:00–09:15 Standup" in s
        assert "todo el día · Cumple de mamá" in s

    def test_sin_eventos_hoy_lo_dice(self):
        assert "sin eventos" in bb.section_today([]).lower()

    def test_proximos_dias_ordena_por_fecha_y_excluye_hoy(self):
        s = bb.section_upcoming(PROXIMOS + [EVENTOS_HOY[0]], HOY)
        assert s.startswith("🔜 *Próximos días*")
        assert s.index("Vuelo MEX-MID") < s.index("Cita dental Regina")
        assert "Standup" not in s
        assert "mañana" in s and "sáb 24-oct 13:30" in s

    def test_sin_proximos_no_hay_seccion(self):
        assert bb.section_upcoming([], HOY) == ""

    def test_vence_esta_semana_lista_todo_lo_que_vence_en_7_dias(self):
        tareas = [
            _task(id=1, title="Pagar Telcel", due_date=date(2026, 10, 20), amount=489.0, source="email"),
            _task(id=2, title="Deck a Lucero", due_date=date(2026, 10, 17)),
            _task(id=3, title="Renovar seguro", due_date=date(2026, 11, 30)),
            _task(id=4, title="Sin fecha"),
        ]
        s = bb.section_due_week(tareas, HOY)
        assert s.startswith("⏰ *Vence esta semana*")
        assert s.index("Deck a Lucero") < s.index("Pagar Telcel")
        assert "$489" in s and "Renovar seguro" not in s and "Sin fecha" not in s

    def test_plan_del_dia_incluye_resumen_bloques_y_prioridades(self):
        plan = {"summary": "Día cargado.", "priorities": ["Deck", "Telcel"], "warnings": []}
        bloques = [{"title": "Bloque consultorio", "start": "10:00", "end": "11:30"}]
        s = bb.section_plan(plan, bloques)
        assert s.startswith("🧭 *Plan del día*") and "Día cargado." in s
        assert "10:00–11:30 Bloque consultorio" in s and "• Deck" in s

    def test_alertas_une_las_del_llm_y_las_del_sistema(self):
        s = bb.section_alerts(["Comida se encima con standup"], ["🔑 Token de Google caducado"])
        assert s.startswith("⚠️ *Alertas*") and "🔑" in s and "Comida" in s
        assert bb.section_alerts([], []) == ""


class TestEnsamble:
    def test_orden_fijo_y_sin_secciones_vacias(self):
        txt = bb.assemble(
            today=HOY,
            section_today_txt=bb.section_today(EVENTOS_HOY),
            upcoming_txt="",
            due_txt="⏰ *Vence esta semana*\n• x",
            pending_txt="📝 *Pendientes*\n• y",
            plan_txt="🧭 *Plan del día*\nz",
            alerts_txt="",
        )
        assert txt.startswith("📅 *Hoy*")
        assert txt.index("⏰") < txt.index("📝") < txt.index("🧭")
        assert "🔜" not in txt and "⚠️" not in txt
        assert "\n\n\n" not in txt
