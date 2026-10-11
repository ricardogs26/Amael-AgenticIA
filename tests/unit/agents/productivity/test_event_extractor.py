"""Crear eventos desde el chat: la parte determinista (sin LLM, sin Google).

Caso real 10-oct-2026: recordatorio de Dentalitos reenviado por WhatsApp —
sáb 24-oct 13:30, Regina, Dra. Luz María Frank Ruiz, consultorio 999 709 4916.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agents.productivity import event_extractor as ex
from agents.productivity.errors import GoogleAuthRevocada

TZ = ZoneInfo("America/Mexico_City")
AHORA = datetime(2026, 10, 11, 12, 0, tzinfo=TZ)   # domingo

DENTALITOS_JSON = {
    "title": "Cita dental — Regina (Dra. Luz María Frank Ruiz)",
    "date": "2026-10-24",
    "time": "13:30",
    "duration_min": None,
    "location": "https://maps.app.goo.gl/oydBgFHdDFEXw71",
    "notes": "Consultorio 999 709 4916 · WhatsApp 999 907 4598",
}


class TestResolveEvent:
    def test_dentalitos_completo(self):
        spec = ex.resolve_event(DENTALITOS_JSON, "agéndalo … sábado 24 octubre 01:30 PM", AHORA)
        assert spec.start == datetime(2026, 10, 24, 13, 30, tzinfo=TZ)
        assert spec.end == datetime(2026, 10, 24, 14, 30, tzinfo=TZ)   # default 60 min
        assert spec.start_iso == "2026-10-24T13:30:00"
        assert "duración" in spec.defaults_used
        assert spec.adjusted_year is False

    def test_sin_hora_pregunta(self):
        parsed = dict(DENTALITOS_JSON, time=None)
        with pytest.raises(ex.EventoIncompleto) as e:
            ex.resolve_event(parsed, "agéndalo", AHORA)
        assert e.value.faltan == ["hora"]

    def test_sin_fecha_ni_hora(self):
        with pytest.raises(ex.EventoIncompleto) as e:
            ex.resolve_event({"title": "x", "date": None, "time": None}, "agéndalo", AHORA)
        assert e.value.faltan == ["fecha", "hora"]

    def test_fecha_pasada_sin_anio_en_el_texto_salta_al_siguiente(self):
        # El remitente dijo «sábado 3 enero» en octubre; el LLM puso 2026-01-03.
        parsed = dict(DENTALITOS_JSON, date="2026-01-03")
        spec = ex.resolve_event(parsed, "agéndalo … sábado 3 enero 01:30 PM", AHORA)
        assert spec.start.year == 2027 and spec.adjusted_year is True

    def test_fecha_pasada_con_anio_explicito_se_respeta(self):
        parsed = dict(DENTALITOS_JSON, date="2026-01-03")
        spec = ex.resolve_event(parsed, "agéndalo … 3 de enero de 2026", AHORA)
        assert spec.start.year == 2026 and spec.adjusted_year is False

    # AHORA es domingo 11-oct-2026.
    @pytest.mark.parametrize("valor,esperado", [
        ("2026-10-24", "2026-10-24"),
        ("hoy", "2026-10-11"),
        ("mañana", "2026-10-12"),
        ("pasado mañana", "2026-10-13"),
        ("jueves", "2026-10-15"),
        ("el jueves", "2026-10-15"),
        ("Domingo", "2026-10-11"),       # hoy es domingo → hoy
        ("sábado", "2026-10-17"),
        ("próximo lunes", "2026-10-12"),
        ("miércoles", "2026-10-14"),
        ("miercoles", "2026-10-14"),
        ("xyz", None), (None, None),
    ])
    def test_fechas_relativas_se_resuelven_en_codigo(self, valor, esperado):
        got = ex._parse_date(valor, AHORA.date())
        assert (got.isoformat() if got else None) == esperado

    @pytest.mark.parametrize("hora,esperado", [
        ("13:30", (13, 30)), ("9", (9, 0)), ("09.15", (9, 15)), ("13h00", (13, 0)),
    ])
    def test_formatos_de_hora(self, hora, esperado):
        assert ex._parse_time(hora) == esperado

    @pytest.mark.parametrize("hora", ["25:00", "13:60", "pm", "", None, "1:30 pm"])
    def test_horas_invalidas(self, hora):
        assert ex._parse_time(hora) is None

    def test_titulo_vacio_usa_default(self):
        spec = ex.resolve_event(dict(DENTALITOS_JSON, title=""), "x", AHORA)
        assert spec.title == ex.DEFAULT_TITLE and "título" in spec.defaults_used

    def test_duracion_fuera_de_rango_usa_default(self):
        spec = ex.resolve_event(dict(DENTALITOS_JSON, duration_min=5000), "x", AHORA)
        assert (spec.end - spec.start).seconds == 3600


class TestFormato:
    def test_confirmacion_dice_que_quedo_agendado(self):
        spec = ex.resolve_event(DENTALITOS_JSON, "agéndalo", AHORA)
        msg = ex.format_confirmation(spec, "https://calendar.google.com/x")
        assert msg.startswith("📅 Agendado: sáb 24-oct-2026, 13:30–14:30")
        assert "Cita dental — Regina" in msg
        assert "📍 https://maps.app.goo.gl" in msg
        assert "999 709 4916" in msg
        assert msg.endswith("https://calendar.google.com/x")

    def test_incompleto_pide_el_dato(self):
        assert "hora" in ex.format_incomplete(["hora"])
        assert "fecha y hora" in ex.format_incomplete(["fecha", "hora"])


# ── El agente: credenciales → extracción → creación → confirmación ───────────

class TestHanielCreateEvent:
    def _agent(self):
        from agents.productivity.agent import HanielAgent
        return HanielAgent.__new__(HanielAgent)

    async def test_flujo_completo_crea_sin_marca_del_planner(self):
        agent = self._agent()
        creado = {"htmlLink": "https://calendar.google.com/evt", "id": "abc"}
        with patch("agents.productivity.vault_credentials.get_user_credentials",
                   return_value=MagicMock()), \
             patch("agents.productivity.event_extractor.extract_event_fields",
                   return_value=DENTALITOS_JSON), \
             patch("agents.productivity.calendar_manager.create_calendar_event",
                   return_value=creado) as crear:
            res = await agent._handle_create_event("ricardo@x.com", "agéndalo …")
        assert res.success, res.error
        assert res.output["response"].startswith("📅 Agendado: sáb 24-oct")
        assert "https://calendar.google.com/evt" in res.output["response"]
        kwargs = crear.call_args.kwargs
        assert kwargs["start_iso"] == "2026-10-24T13:30:00"
        # Un evento del usuario NO lleva la marca del planner: el brief borra
        # cada mañana todo lo que lleve esa marca.
        assert kwargs["planner_mark"] is False

    async def test_sin_hora_pregunta_sin_crear(self):
        agent = self._agent()
        with patch("agents.productivity.vault_credentials.get_user_credentials",
                   return_value=MagicMock()), \
             patch("agents.productivity.event_extractor.extract_event_fields",
                   return_value=dict(DENTALITOS_JSON, time=None)), \
             patch("agents.productivity.calendar_manager.create_calendar_event") as crear:
            res = await agent._handle_create_event("ricardo@x.com", "agéndalo …")
        assert res.success and "hora" in res.output["response"]
        crear.assert_not_called()

    async def test_sin_credenciales_dice_como_conectar(self):
        agent = self._agent()
        with patch("agents.productivity.vault_credentials.get_user_credentials",
                   return_value=None):
            res = await agent._handle_create_event("ricardo@x.com", "agéndalo")
        assert not res.success
        assert "amael-ia.richardx.dev" in res.error

    async def test_token_revocado_dice_como_reautorizar(self):
        agent = self._agent()
        with patch("agents.productivity.vault_credentials.get_user_credentials",
                   return_value=MagicMock()), \
             patch("agents.productivity.event_extractor.extract_event_fields",
                   return_value=DENTALITOS_JSON), \
             patch("agents.productivity.calendar_manager.create_calendar_event",
                   side_effect=GoogleAuthRevocada("ricardo@x.com", "invalid_grant")):
            res = await agent._handle_create_event("ricardo@x.com", "agéndalo …")
        assert not res.success
        assert "amael-ia.richardx.dev" in res.error
        assert "invalid_grant" not in res.error   # el usuario no necesita el error crudo

    async def test_get_events_con_token_revocado_tambien_explica(self):
        agent = self._agent()
        with patch("agents.productivity.vault_credentials.get_user_credentials",
                   return_value=MagicMock()), \
             patch("agents.productivity.calendar_manager.get_todays_events",
                   side_effect=GoogleAuthRevocada("ricardo@x.com", "invalid_grant")):
            res = await agent._handle_get_events("ricardo@x.com")
        assert not res.success and "amael-ia.richardx.dev" in res.error
