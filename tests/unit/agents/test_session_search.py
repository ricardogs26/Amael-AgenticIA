"""
Tests de la búsqueda de historial (Zaphkiel `search_history`).

El principio bajo prueba: mensajes CRUDOS, sin resumen del LLM (lección del
session search de Hermes), y el user_id jamás sale del texto del usuario.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import agents.memory_agent.agent as zaph
from agents.memory_agent.agent import _significant_terms

# ── Extracción de términos ────────────────────────────────────────────────────

@pytest.mark.parametrize("query,esperado", [
    ("¿qué te dije del trader la semana pasada?", ["trader"]),
    ("recuerda lo que hablamos sobre vault y el unseal", ["vault", "unseal"]),
    ("what did I tell you about kubernetes?", ["kubernetes"]),
])
def test_extrae_lo_que_discrimina_y_tira_el_andamiaje(query, esperado):
    assert _significant_terms(query) == esperado


def test_pregunta_sin_sustancia_no_produce_terminos():
    """«qué te dije» sin tema debe pedir palabra clave, no barrer todo."""
    assert _significant_terms("¿qué te dije?") == []


def test_maximo_cuatro_terminos():
    q = "trader vault kubernetes grafana prometheus redis postgres"
    assert len(_significant_terms(q)) == 4


# ── Acción search_history ─────────────────────────────────────────────────────

def _agente():
    from core.agent_base import AgentContext
    ctx = AgentContext(user_id="u@x.com", conversation_id="", request_id="t",
                       llm=None)
    return zaph.ZaphkielAgent(ctx)


async def test_devuelve_mensajes_crudos_con_fecha_y_autor(monkeypatch):
    filas = [
        ("el trader compró XLE a 91.30", "assistant",
         datetime(2026, 8, 1, 14, 3), "Trading"),
        ("¿cómo va el trader hoy?", "user",
         datetime(2026, 8, 1, 14, 2), "Trading"),
    ]
    monkeypatch.setattr(zaph, "_search_messages", lambda u, t, k: filas)

    res = await _agente().execute(
        {"action": "search_history", "user_id": "u@x.com",
         "query": "qué te dije del trader"}
    )
    assert res.success
    texto = res.output["response"]
    # Citas textuales, no paráfrasis: el contenido crudo debe estar ahí.
    assert "el trader compró XLE a 91.30" in texto
    assert "2026-08-01 14:03" in texto
    assert "Amael" in texto and "tú" in texto
    assert res.output["matches"] == 2


async def test_sin_resultados_lo_dice_sin_inventar(monkeypatch):
    monkeypatch.setattr(zaph, "_search_messages", lambda u, t, k: [])
    res = await _agente().execute(
        {"action": "search_history", "user_id": "u@x.com",
         "query": "qué te dije de bitcoin"}
    )
    assert res.success
    assert "No encontré" in res.output["response"]


async def test_sin_terminos_pide_palabra_clave(monkeypatch):
    llamado = []
    monkeypatch.setattr(zaph, "_search_messages",
                        lambda u, t, k: llamado.append(1) or [])
    res = await _agente().execute(
        {"action": "search_history", "user_id": "u@x.com", "query": "¿qué te dije?"}
    )
    assert res.success
    assert not llamado, "no debe barrer la tabla sin términos que discriminen"


async def test_sin_user_id_falla():
    res = await _agente().execute(
        {"action": "search_history", "user_id": "", "query": "trader"}
    )
    assert not res.success


def test_el_intent_memory_va_directo_a_zaphkiel():
    from orchestration.agent_dispatcher import _DIRECT_DISPATCH, _PIPELINE_INTENTS
    assert _DIRECT_DISPATCH.get("memory") == "zaphkiel"
    assert "memory" not in _PIPELINE_INTENTS


async def test_los_mensajes_largos_se_recortan_por_cita(monkeypatch):
    """Cada cita se recorta a ~220 chars — el CONJUNTO no se trunca."""
    filas = [("x" * 1000, "user", datetime(2026, 8, 1, 10, 0), "T")]
    monkeypatch.setattr(zaph, "_search_messages", lambda u, t, k: filas)
    res = await _agente().execute(
        {"action": "search_history", "user_id": "u@x.com", "query": "trader"}
    )
    linea = next(ln for ln in res.output["response"].splitlines() if "«" in ln)
    assert len(linea) < 300
    assert "…" in linea


async def test_ignora_el_contexto_inyectado_por_chat(monkeypatch):
    """
    Regresión del primer E2E: chat.py antepone «[Contexto de sesiones
    anteriores]…» y los términos salían del prefijo («sesiones», «anteriores»)
    en vez de la pregunta. Solo cuenta lo que el usuario escribió.
    """
    capturado = {}

    def fake(u, terminos, k):
        capturado["terminos"] = terminos
        return []

    monkeypatch.setattr(zaph, "_search_messages", fake)
    pregunta = (
        "[Contexto de sesiones anteriores]\nEl 2026-08-06 hablamos de contexto "
        "y sesiones anteriores.\n\n[Pregunta actual]\nqué te dije del trader"
    )
    await _agente().execute(
        {"action": "search_history", "user_id": "u@x.com", "query": pregunta}
    )
    assert capturado["terminos"] == ["trader"]
