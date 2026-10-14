"""Entrega F: responder CITANDO el brief (o un nudge de pendientes) por
WhatsApp cierra el ciclo: «ya pagué la línea» → Cassiel con el brief como
contexto, sin pasar por el router (el intent lo decide el código)."""
from __future__ import annotations

from interfaces.api.routers import chat

BRIEF = ("📅 *Brief del día*\n\n📅 *Hoy*\n• 09:00–09:15 Standup\n\n"
         "⏰ *Vence esta semana*\n• sáb 17 · #5 Deck a Lucero\n• mar 20 · #7 Pagar factura Telcel octubre · $489 (del correo)\n\n"
         "📝 *Pendientes*\n🔴 #2 Llamar al plomero — 2 días de atraso")
NUDGE = "⏰ *Pendientes*\n\nTienes pendientes que necesitan atención:\n• #2 Llamar al plomero — para hoy"


class TestDeteccion:
    def test_brief_y_nudge_se_reconocen(self):
        assert chat._is_brief_quote(BRIEF)
        assert chat._is_brief_quote(NUDGE)

    def test_otro_mensaje_citado_no(self):
        assert not chat._is_brief_quote("Hola, ¿cómo estás?")
        assert not chat._is_brief_quote("")
        assert not chat._is_brief_quote(None)


class TestMerge:
    def test_la_pregunta_va_al_final_con_el_brief_como_contexto(self):
        m = chat._merge_brief_quote("ya pagué la de Telcel", BRIEF)
        assert m.startswith("[Contexto: el usuario responde citando el brief del día:")
        assert m.rstrip().endswith("ya pagué la de Telcel")
        assert "#7 Pagar factura Telcel" in m

    def test_el_brief_se_recorta(self):
        largo = BRIEF + "\n" + ("x" * 5000)
        assert len(chat._merge_brief_quote("ok", largo)) < 2000


async def test_citar_el_brief_rutea_a_cassiel_sin_router(monkeypatch):
    monkeypatch.setattr(chat, "_pop_pending_paste", lambda u: None)
    monkeypatch.setattr("agents.scheduler.agent.pop_followup", lambda u: None)

    async def _no_router(self, q):
        raise AssertionError("no debe pasar por el router")
    monkeypatch.setattr("orchestration.agent_router.AgentRouter.route", _no_router)

    q, d = await chat._route_with_followup("u@x.com", "ya pagué la de Telcel", quoted_text=BRIEF)
    assert d.intent == "reminder" and d.routing_reason == "brief_reply"
    assert "Telcel" in q and q.rstrip().endswith("ya pagué la de Telcel")


async def test_citar_otra_cosa_sigue_el_ruteo_normal(monkeypatch):
    monkeypatch.setattr(chat, "_pop_pending_paste", lambda u: None)
    monkeypatch.setattr("agents.scheduler.agent.pop_followup", lambda u: None)
    q, d = await chat._route_with_followup("u@x.com", "¿qué clima hace?", quoted_text="hola")
    assert q == "¿qué clima hace?" and d.routing_reason != "brief_reply"


def test_chat_request_acepta_quoted_text():
    r = chat.ChatRequest(prompt="ya", quoted_text=BRIEF)
    assert r.quoted_text == BRIEF
