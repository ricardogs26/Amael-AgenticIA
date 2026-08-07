"""
Tests de Cassiel (scheduler conversacional).

Lo crítico: (1) el ruteo — «recuérdame» debe caer en Cassiel y NO en la regla
de memoria, que ya atrapaba recuerda|recordar; (2) la validación del schedule
vive en código y rechaza lo que el LLM invente mal; (3) el claim fija la
siguiente ejecución en la misma transacción para que dos réplicas no dupliquen
la entrega.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.scheduler import storage
from orchestration.agent_router import AgentRouter

# ── Ruteo ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("frase", [
    "recuérdame revisar el PR todos los lunes a las 9",
    "recuerdame tomar agua cada hora",
    "mándame el resumen del trader todos los días a las 3pm",
    "envíame el estado del cluster cada lunes",
    "¿qué tareas programadas tengo?",
    "mis recordatorios",
    "recordatorio para mañana: llamar al banco",
])
async def test_frases_de_recordatorio_rutean_a_cassiel(frase):
    decision = await AgentRouter().route(frase)
    assert decision.intent == "reminder", (
        f"{frase!r} ruteó a {decision.intent!r} — si cayó en 'memory' es la "
        f"regla recuerda|recordar comiéndose los recordatorios"
    )


@pytest.mark.parametrize("frase,intent", [
    # La regla de memoria debe seguir viva para consultas de memoria reales.
    # (con "recuerda" en imperativo: el patrón de memoria nunca atrapó
    # "recuerdas" — el \b no corta antes de la s; eso ya caía a general)
    ("recuerda lo que te dije del proyecto", "memory"),
    # Calendario ≠ recordatorio: agenda va a Haniel.
    ("agenda una reunión con Marco el jueves", "productivity"),
])
async def test_los_intents_vecinos_no_se_rompen(frase, intent):
    assert (await AgentRouter().route(frase)).intent == intent


def test_cassiel_esta_en_dispatch_directo():
    from orchestration.agent_dispatcher import _DIRECT_DISPATCH
    assert _DIRECT_DISPATCH.get("reminder") == "cassiel"


def test_cassiel_se_registra():
    from agents.base.agent_registry import AgentRegistry, register_all_agents
    register_all_agents()
    assert "cassiel" in AgentRegistry.names()


# ── Validación de schedule (en código, no en el LLM) ──────────────────────────

def test_cron_valido_produce_futuro_recurrente():
    cuando, one_shot = storage.compute_next_run("0 15 * * *", "America/Mexico_City")
    assert one_shot is False
    assert cuando > datetime.now(UTC)


def test_iso_futuro_es_one_shot():
    futuro = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    cuando, one_shot = storage.compute_next_run(futuro, "UTC")
    assert one_shot is True


def test_iso_pasado_se_rechaza():
    with pytest.raises(ValueError, match="ya pasó"):
        storage.compute_next_run("2020-01-01T09:00:00", "America/Mexico_City")


@pytest.mark.parametrize("malo", [
    "mañana a las 9",          # lenguaje natural: el LLM debía traducirlo
    "* * * *",                 # 4 campos
    "99 99 * * *",             # fuera de rango
    "'; DROP TABLE user_jobs", # basura
    "",
])
def test_schedules_invalidos_se_rechazan_con_mensaje_legible(malo):
    with pytest.raises(ValueError):
        storage.compute_next_run(malo, "America/Mexico_City")


def test_timezone_desconocida_se_rechaza():
    with pytest.raises(ValueError, match="Zona horaria"):
        storage.compute_next_run("0 9 * * *", "Marte/Cydonia")


def test_el_cron_respeta_la_zona_del_usuario():
    """0 15 en CDMX no es 0 15 en UTC — el bug clásico del day-planner."""
    mx,  _ = storage.compute_next_run("0 15 * * *", "America/Mexico_City")
    utc, _ = storage.compute_next_run("0 15 * * *", "UTC")
    assert mx != utc


# ── Cassiel: validación de acciones sin LLM ───────────────────────────────────

class _CassielSinLLM:
    """Instancia mínima para ejercitar _apply sin AgentContext ni Ollama."""
    def __init__(self):
        from agents.scheduler.agent import CassielAgent
        self._apply = CassielAgent._apply.__get__(self)


def test_accion_desconocida_no_truena(monkeypatch):
    agente = _CassielSinLLM()
    out = agente._apply({"action": "explotar"}, "user@x.com", "UTC")
    assert "desconocida" in out.lower()


def test_create_sin_schedule_pide_el_cuando(monkeypatch):
    agente = _CassielSinLLM()
    out = agente._apply(
        {"action": "create", "title": "x", "prompt": "y", "schedule": ""},
        "user@x.com", "UTC",
    )
    assert "cuándo" in out.lower() or "falta" in out.lower()


def test_unclear_devuelve_la_pregunta():
    agente = _CassielSinLLM()
    out = agente._apply(
        {"action": "unclear", "clarification": "¿A qué hora?"}, "user@x.com", "UTC"
    )
    assert out == "¿A qué hora?"


# ── find_job: ambigüedad ──────────────────────────────────────────────────────

def test_find_job_ambiguo_lista_candidatos(monkeypatch):
    ahora = datetime.now(UTC)
    jobs = [
        storage.Job(id=1, user_id="u", title="resumen trader", prompt="p",
                    schedule="0 9 * * *", timezone="UTC", delivery="whatsapp",
                    enabled=True, one_shot=False, next_run_at=ahora),
        storage.Job(id=2, user_id="u", title="resumen cluster", prompt="p",
                    schedule="0 9 * * *", timezone="UTC", delivery="whatsapp",
                    enabled=True, one_shot=False, next_run_at=ahora),
    ]
    monkeypatch.setattr(storage, "list_jobs", lambda u, include_disabled=True: jobs)
    with pytest.raises(ValueError, match="varias"):
        storage.find_job("u", "resumen")
    assert storage.find_job("u", "trader").id == 1
    assert storage.find_job("u", "2").id == 2
    assert storage.find_job("u", "inexistente") is None


# ── Runner ────────────────────────────────────────────────────────────────────

async def test_run_job_espera_al_router_y_entrega(monkeypatch):
    """
    Regresión: `AgentRouter.route` es corutina y el runner la pasaba sin await
    al dispatcher — el primer job real murió con «'coroutine' object has no
    attribute 'intent'». Este test ejecuta el camino completo con dobles.
    """
    from agents.scheduler import runner

    class _Router:
        async def route(self, q):
            from orchestration.agent_router import RoutingDecision
            return RoutingDecision(intent="chat", agents=["chat"],
                                   confidence=0.9, routing_reason="test")

    entregado = {}

    class _Dispatcher:
        async def dispatch(self, **kw):
            assert hasattr(kw["routing_decision"], "intent"), \
                "el runner pasó una corutina sin esperar"
            return {"final_answer": "hola"}

    monkeypatch.setattr("orchestration.agent_router.AgentRouter", _Router)
    monkeypatch.setattr("orchestration.agent_dispatcher.AgentDispatcher", _Dispatcher)
    monkeypatch.setattr("interfaces.api.routers.chat._build_tools_map", lambda u: {})
    monkeypatch.setattr(runner, "_deliver_whatsapp",
                        lambda job, text: entregado.update(texto=text))

    job = storage.Job(id=99, user_id="u@x.com", title="t", prompt="saluda",
                      schedule="0 9 * * *", timezone="UTC", delivery="whatsapp",
                      enabled=True, one_shot=False, next_run_at=datetime.now(UTC))
    await runner._run_job(job)
    assert entregado["texto"] == "hola"


async def test_run_job_sin_respuesta_truena(monkeypatch):
    """Un dispatcher que devuelve vacío debe registrarse como error, no como ok."""
    from agents.scheduler import runner

    class _Router:
        async def route(self, q):
            from orchestration.agent_router import RoutingDecision
            return RoutingDecision(intent="chat", agents=["chat"],
                                   confidence=0.9, routing_reason="test")

    class _Dispatcher:
        async def dispatch(self, **kw):
            return {"final_answer": ""}

    monkeypatch.setattr("orchestration.agent_router.AgentRouter", _Router)
    monkeypatch.setattr("orchestration.agent_dispatcher.AgentDispatcher", _Dispatcher)
    monkeypatch.setattr("interfaces.api.routers.chat._build_tools_map", lambda u: {})

    job = storage.Job(id=99, user_id="u@x.com", title="t", prompt="x",
                      schedule="0 9 * * *", timezone="UTC", delivery="none",
                      enabled=True, one_shot=False, next_run_at=datetime.now(UTC))
    with pytest.raises(RuntimeError, match="no devolvió respuesta"):
        await runner._run_job(job)
