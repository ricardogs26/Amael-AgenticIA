import pytest

from agents.planner.agent import _apply_fast_paths
from knowledge.intencion import es_consulta_de_conocimiento as es
from orchestration.agent_dispatcher import _PIPELINE_INTENTS
from orchestration.agent_router import AgentRouter

SI = ["¿Qué decidimos sobre el RAG gobernado de Amael?",
      "¿Por qué elegimos qwen3.5:9b para Amael Code?",
      "¿Cuál es el estado de Kanan?",
      "Qué notas tengo sobre el Xid de la GPU",
      "¿Qué es Pixan Labs?",
      "pendientes de shikigami"]
NO = ["Hola, buenos días", "reinicia raphael", "agenda una cita con kanan mañana",
      "¿Qué hora es en Tokio?", "¿Cómo está el clima?", "Escribe una función en Python"]


@pytest.mark.parametrize("q", SI)
def test_detecta(q):
    assert es(q)


@pytest.mark.parametrize("q", NO)
def test_no_detecta(q):
    assert not es(q)


@pytest.mark.parametrize("q", SI)
async def test_el_router_va_al_pipeline_sin_llamar_al_modelo(q):
    d = await AgentRouter().route(q)
    assert d.intent == "knowledge" and d.intent in _PIPELINE_INTENTS
    assert not d.llm_classified


def test_el_plan_empieza_consultando():
    plan = _apply_fast_paths(SI[2], ["REASONING: responder"])
    assert plan[0].startswith("KNOWLEDGE:") and SI[2] in plan[0]
    assert plan[1:] == ["REASONING: responder"]


def test_no_duplica_el_paso():
    plan = ["KNOWLEDGE: x", "REASONING: y"]
    assert _apply_fast_paths(SI[0], plan) == plan


def test_otra_pregunta_no_cambia_el_plan():
    assert _apply_fast_paths("hola", ["REASONING: y"]) == ["REASONING: y"]
