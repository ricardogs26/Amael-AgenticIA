"""
Tests de Phanuel (QA) — catálogo de pruebas y ejecución multi-repo.

Lo que se protege: (1) el catálogo se carga y llega al system prompt, (2) del
lenguaje natural se resuelve el componente correcto, (3) el default sigue siendo
el backend (compatibilidad con el comportamiento previo de un solo repo).
"""
from __future__ import annotations

import agents.qa.agent as qa


def _agente():
    from core.agent_base import AgentContext
    ctx = AgentContext(user_id="u@x.com", conversation_id="", request_id="t", llm=None)
    return qa.QAAgent(ctx)


# ── Catálogo (la «memoria») ────────────────────────────────────────────────────

def test_el_catalogo_se_carga_al_arrancar():
    """qa_knowledge.md debe existir y no estar vacío — es la memoria de Phanuel."""
    assert len(qa._QA_KNOWLEDGE) > 500
    assert "trader-service" in qa._QA_KNOWLEDGE
    assert "amael-agentic-backend" in qa._QA_KNOWLEDGE


def test_el_catalogo_entra_al_system_prompt():
    assert "CATÁLOGO DE PRUEBAS" in qa._SYSTEM_CONVERSATIONAL
    assert "cuándo correr qué" in qa._SYSTEM_CONVERSATIONAL.lower() or \
           "cuándo correr" in qa._SYSTEM_CONVERSATIONAL


def test_las_llaves_del_markdown_van_escapadas():
    """El KB lleva llaves de comandos; sin escapar rompen PromptTemplate."""
    # El KB cargado no debe tener llaves simples sin escapar (todas dobladas).
    import re
    solteras = re.findall(r"(?<!\{)\{(?!\{)", qa._QA_KNOWLEDGE)
    assert not solteras, "hay llaves sin escapar en el KB — romperán el prompt"


# ── Resolución de componente ───────────────────────────────────────────────────

def test_resuelve_el_trader_por_lenguaje_natural():
    agente = _agente()
    _, repo, _ = agente._resolve_component("corre los tests del trader")
    assert repo == "trader-service"


def test_resuelve_el_backend_y_sus_sinonimos():
    agente = _agente()
    for frase in ["corre los tests del backend", "ejecuta las pruebas de amael",
                  "run tests agentic"]:
        _, repo, _ = agente._resolve_component(frase)
        assert repo == "Amael-AgenticIA", frase


def test_raphael_y_camael_comparten_la_suite_del_backend():
    agente = _agente()
    for frase in ["tests de raphael", "pruebas de camael"]:
        _, repo, _ = agente._resolve_component(frase)
        assert repo == "Amael-AgenticIA", frase


def test_sin_componente_explicito_default_es_backend():
    """Compatibilidad: antes solo existía un repo; el default no debe cambiar."""
    agente = _agente()
    _, repo, ref = agente._resolve_component("corre los tests")
    assert repo == "Amael-AgenticIA"
    assert ref == "main"


def test_todos_los_componentes_del_registro_apuntan_a_repos_reales():
    """El registro no debe listar un repo que no existe."""
    repos = {t[1] for t in qa._COMPONENTS.values()}
    assert repos == {"Amael-AgenticIA", "trader-service"}


# ── Modo ───────────────────────────────────────────────────────────────────────

def test_el_modo_run_se_detecta():
    agente = _agente()
    assert agente._detect_mode("corre los tests del trader") == "run"
    assert agente._detect_mode("¿cuál es el estado de los tests?") == "status"
    assert agente._detect_mode("qué cubre el test del analyzer") == "conversational"
