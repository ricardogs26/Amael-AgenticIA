"""Ruteo determinista de calendario y memoria (orchestration/agent_router.py).

Caso real 10-oct-2026: el recordatorio de Dentalitos («…te recuerda tu
cita…») cayó en `memory` por el regex `recuerdas?`, y «Puedes ayudarme a
agendar esta cita…» no tenía a dónde ir porque no existía la acción de crear
eventos. Estos tests fijan: (1) «recuerda» en tercera persona no es memoria,
(2) una orden de agendar va a `calendar_create` aunque el texto fusionado con
el reenvío sea largo (la regla de productividad se salta por longitud)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestration import agent_router
from tests.unit.test_paste_pending import DENTALITOS


@pytest.fixture
def router():
    r = agent_router.AgentRouter()
    # Sin LLM: si ninguna regla matchea, queda `None` → default general.
    with patch.object(agent_router.AgentRouter, "_route_with_llm", AsyncMock(return_value=None)):
        yield r


class TestCalendarCreate:
    @pytest.mark.parametrize("texto", [
        "agéndalo",
        "agendalo por favor",
        "agéndame la cita del dentista el sábado 24 a la 1:30 pm",
        "agenda esta cita",
        "Puedes ayudarme a agendar esta cita para venir con Regis al dentista.",
        "ponlo en mi calendario",
        "ponme en el calendario la junta del lunes a las 10",
        "crea un evento para el viernes a las 5",
        "mete en mi agenda la cita con el pediatra el jueves 9am",
        "añade a mi calendario la reunión de mañana a las 4",
    ])
    async def test_ordenes_de_agendar(self, router, texto):
        d = await router.route(texto)
        assert d.intent == "calendar_create", d
        assert d.agents == ["productivity"]

    async def test_texto_fusionado_largo_sigue_siendo_calendar_create(self, router):
        from interfaces.api.routers.chat import _merge_paste
        merged = _merge_paste("agéndalo", DENTALITOS)
        assert len(merged) > 400
        d = await router.route(merged)
        assert d.intent == "calendar_create", d

    async def test_leer_agenda_sigue_siendo_productivity(self, router):
        d = await router.route("¿qué tengo en mi agenda hoy?")
        assert d.intent == "productivity"

    async def test_necesito_agendar_sigue_siendo_pendiente(self, router):
        # «necesito agendar X» es una tarea pendiente (Cassiel), no un evento.
        d = await router.route("necesito agendar cita con el dentista")
        assert d.intent == "reminder"

    async def test_recuerdame_sigue_siendo_reminder(self, router):
        d = await router.route("recuérdame la cita mañana a las 9")
        assert d.intent == "reminder"


class TestMemoriaSegundaPersona:
    async def test_te_recuerda_tu_cita_no_es_memoria(self, router):
        d = await router.route(DENTALITOS)
        assert d.intent != "memory", d

    @pytest.mark.parametrize("texto", [
        "¿recuerdas lo que te dije del dentista?",
        "¿te acuerdas de la cita?",
        "¿qué te dije sobre Regina?",
        "busca en el historial lo de Dentalitos",
    ])
    async def test_preguntas_de_memoria(self, router, texto):
        d = await router.route(texto)
        assert d.intent == "memory", d
