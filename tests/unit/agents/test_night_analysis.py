"""Análisis nocturno de pendientes (spec Fase 2 §4) y sección «Pendientes»
del brief. Todo lo decidible es código: el LLM solo señala ids y una
pregunta; el código valida ids, ordena y redacta."""
from __future__ import annotations

from datetime import UTC, date, datetime

from agents.scheduler import night_analysis as na
from agents.scheduler import tasks_storage as ts

HOY = date(2026, 10, 14)


def _task(**kw):
    base = dict(
        id=1, user_id="u@x.com", title="t", description="", category="personal",
        priority="media", estimated_minutes=None, due_date=None, status="pending",
        needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC), completed_at=None,
    )
    base.update(kw)
    return ts.Task(**base)


TAREAS = [
    _task(id=1, title="Pagar factura Telcel octubre", priority="media",
          due_date=date(2026, 10, 20), source="email", amount=489.0,
          source_sender="facturacion@telcel.com"),
    _task(id=2, title="Llamar al plomero", priority="alta", due_date=date(2026, 10, 12)),
    _task(id=3, title="Leer Rewired", priority="baja",
          created_at=datetime(2026, 9, 20, tzinfo=UTC)),
    _task(id=4, title="Cita dental Regina", priority="media", due_date=date(2026, 10, 14),
          source="email"),
    _task(id=5, title="Comprar foco", priority="baja"),
]


class TestRenderAnalisis:
    def test_ids_validos_se_redactan_y_los_inventados_se_ignoran(self):
        parsed = {"top": [2, 99], "stalled": [3], "question": "¿El plomero es para la fuga de la cocina?"}
        txt = na.render_analysis(parsed, TAREAS, HOY)
        assert "Llamar al plomero" in txt
        assert "99" not in txt
        assert "Leer Rewired" in txt and "estancad" in txt.lower()
        assert "¿El plomero es para la fuga de la cocina?" in txt

    def test_sin_nada_util_devuelve_vacio(self):
        # Solo tareas con fecha: ninguna cuenta como estancada por código.
        con_fecha = [t for t in TAREAS if t.due_date is not None]
        assert na.render_analysis({"top": [], "stalled": [], "question": None}, con_fecha, HOY) == ""
        assert na.render_analysis("basura", con_fecha, HOY) == ""

    def test_solo_una_pregunta_aunque_el_llm_mande_lista(self):
        parsed = {"top": [], "stalled": [], "question": ["¿a?", "¿b?"]}
        txt = na.render_analysis(parsed, TAREAS, HOY)
        assert "¿a?" in txt and "¿b?" not in txt

    def test_estancadas_se_calculan_en_codigo_si_el_llm_no_las_da(self):
        # Sin due_date y creada hace >7 días → estancada aunque el LLM no lo diga.
        txt = na.render_analysis({"top": [1]}, TAREAS, HOY)
        assert "Leer Rewired" in txt


class TestSeccionPendientes:
    def test_sin_tareas_no_hay_seccion(self):
        assert na.build_pending_section([], HOY, analysis="") == ""

    def test_top3_orden_determinista_con_atraso_y_monto(self):
        txt = na.build_pending_section(TAREAS, HOY, analysis="")
        lineas = [ln for ln in txt.splitlines() if ln.startswith(("🔴", "🟡", "•"))]
        # vencida (plomero, 2 días) > hoy (cita) > alta sin fecha… media con fecha (Telcel)
        assert lineas[0].startswith("🔴 #2") and "2 día" in lineas[0]
        assert lineas[1].startswith("🟡 #4") and "hoy" in lineas[1]
        assert "#1" in lineas[2] and "$489" in lineas[2] and "6 días" in lineas[2]
        assert len(lineas) == 3
        assert "y 2 más" in txt

    def test_origen_email_se_marca(self):
        txt = na.build_pending_section(TAREAS, HOY, analysis="")
        assert "(del correo)" in txt

    def test_el_analisis_nocturno_se_anexa_si_existe(self):
        txt = na.build_pending_section(TAREAS, HOY, analysis="💡 Empieza por el plomero.")
        assert txt.rstrip().endswith("💡 Empieza por el plomero.")

    def test_encabezado(self):
        assert na.build_pending_section(TAREAS, HOY, analysis="").startswith("📝 *Pendientes*")
