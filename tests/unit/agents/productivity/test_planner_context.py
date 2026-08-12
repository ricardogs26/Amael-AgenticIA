"""El planificador diario debe conocer al usuario y no reorganizarle el día por
descarte.

Contexto (12-ago-2026): el brief generaba «Bloque de concentración» y «Descanso»
en abstracto porque el prompt solo recibía fecha, eventos y correos — ni siquiera
leía los hechos que la memoria ya tenía destilados del usuario.
"""

from agents.productivity import day_planner
from agents.productivity.agent import HanielAgent
from core.agent_base import AgentContext


def _agente() -> HanielAgent:
    return HanielAgent(AgentContext(
        user_id="quien@sea.com", conversation_id="c1", request_id="r1", llm=None,
    ))


def test_una_consulta_ambigua_lee_en_vez_de_reorganizar():
    """Escribir sin que lo pidan es lo que hace daño: «¿qué tengo pendiente?»
    no trae ninguna palabra clave y antes creaba el plan completo del día."""
    agente = _agente()
    assert agente._infer_action("¿qué tengo pendiente para mañana?") == "get_events"


def test_pedir_el_plan_explicitamente_si_reorganiza():
    agente = _agente()
    for frase in ["organiza mi día", "planifica mi jornada", "hazme un plan"]:
        assert agente._infer_action(frase) == "organize_day", frase


def test_el_correo_y_el_calendario_siguen_ruteando_igual():
    agente = _agente()
    assert agente._infer_action("revisa mi correo") == "get_emails"
    assert agente._infer_action("¿qué eventos tengo?") == "get_events"


def test_el_prompt_del_planner_incluye_los_hechos_del_usuario():
    prompt = day_planner._build_planning_prompt(
        date="2026-08-12 (Wednesday)",
        events=[],
        emails=[],
        perfil="Hechos confirmados sobre el usuario (aplícalos siempre):\n- trabaja en Kubernetes",
    )

    assert "trabaja en Kubernetes" in prompt
    # No basta con pegar los hechos: el modelo tiene que saber qué hacer con ellos.
    assert "objetivo" in prompt.lower()


def test_sin_hechos_el_prompt_no_queda_con_una_seccion_vacia(monkeypatch):
    """Una sección «OBJETIVOS:» en blanco invita al LLM a inventarlos."""
    prompt = day_planner._build_planning_prompt(
        date="2026-08-12 (Wednesday)", events=[], emails=[], perfil="",
    )

    assert "OBJETIVOS Y CONTEXTO DEL USUARIO" not in prompt


def test_el_perfil_se_lee_best_effort(monkeypatch):
    """Si la memoria falla, el brief tiene que salir igual: es un cron de las
    7 am, no una consulta interactiva que el usuario pueda reintentar."""
    def _explota(_user):
        raise RuntimeError("qdrant caído")

    monkeypatch.setattr(day_planner, "_render_profile_block", _explota)
    assert day_planner._perfil_de("quien@sea.com") == ""
