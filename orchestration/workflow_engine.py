"""
WorkflowEngine — compila y cachea el grafo LangGraph de la plataforma.

Migrado desde backend-ia/agents/orchestrator.py.
El grafo se compila una vez y se reutiliza en todos los requests.
tools_map se inyecta por request via AgentState (P5-2).

Flujo:
  planner → grouper → batch_executor (loop) → supervisor
      ↑                                            │
      └──────── REPLAN (max 1 retry) ─────────────┘
                                                   │
                                                ACCEPT → END
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from agents.executor.agent import batch_executor_node
from agents.planner.agent import grouper_node, planner_node
from agents.supervisor.agent import supervisor_node
from agents.supervisor.quality_scorer import MAX_RETRIES
from core.constants import MAX_GRAPH_ITERATIONS
from observability.metrics import ORCHESTRATOR_MAX_STEPS_HIT_TOTAL
from orchestration.state import AgentState

logger = logging.getLogger("orchestration.workflow")

# Cache global del grafo compilado — un grafo por configuración de redis_client
_WORKFLOW_CACHE: Any | None = None


def _compile_graph(redis_client=None):
    """
    Compila el grafo LangGraph.

    Nodos:
      planner        — genera el plan
      grouper        — agrupa en batches
      batch_executor — ejecuta batches (loop)
      supervisor     — evalúa calidad

    Edges condicionales:
      batch_executor → batch_executor  (más batches pendientes)
      batch_executor → supervisor       (plan completo)
      supervisor     → planner          (REPLAN)
      supervisor     → END              (ACCEPT o max retries)
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("planner",        planner_node)
    workflow.add_node("grouper",        grouper_node)
    workflow.add_node("batch_executor", batch_executor_node)
    workflow.add_node(
        "supervisor",
        lambda state: supervisor_node(state, redis_client=redis_client),
    )

    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "grouper")
    workflow.add_edge("grouper", "batch_executor")

    def should_continue(state: AgentState) -> str:
        current = state.get("current_batch", 0)
        total   = len(state.get("batches", []))
        if current >= MAX_GRAPH_ITERATIONS:
            logger.warning(
                f"[workflow] MAX_GRAPH_ITERATIONS ({MAX_GRAPH_ITERATIONS}) alcanzado. "
                "Forzando fin."
            )
            ORCHESTRATOR_MAX_STEPS_HIT_TOTAL.inc()
            return "supervisor"
        return "batch_executor" if current < total else "supervisor"

    workflow.add_conditional_edges(
        "batch_executor",
        should_continue,
        {"batch_executor": "batch_executor", "supervisor": "supervisor"},
    )

    def supervisor_routing(state: AgentState) -> str:
        decision    = state.get("supervisor_decision", "ACCEPT")
        retry_count = state.get("retry_count", 0)
        if decision == "REPLAN" and retry_count <= MAX_RETRIES:
            logger.info(f"[workflow] Supervisor solicitó REPLAN (retry #{retry_count}).")
            return "planner"
        return END

    workflow.add_conditional_edges(
        "supervisor",
        supervisor_routing,
        {"planner": "planner", END: END},
    )

    return workflow.compile()


def get_workflow(redis_client=None):
    """
    Retorna el grafo compilado (singleton por proceso).

    El grafo se compila en la primera llamada y se cachea.
    Todas las llamadas posteriores retornan la instancia cacheada.
    Esto es seguro porque tools_map se inyecta por request en AgentState.
    """
    global _WORKFLOW_CACHE
    if _WORKFLOW_CACHE is None:
        logger.info("[workflow] Compilando grafo LangGraph (primera vez)...")
        _WORKFLOW_CACHE = _compile_graph(redis_client=redis_client)
        logger.info("[workflow] Grafo compilado y cacheado.")
    return _WORKFLOW_CACHE


# Alias de compatibilidad con backend-ia/agents/orchestrator.py
def get_orchestrator(redis_client=None):
    return get_workflow(redis_client)


def create_orchestrator(llm=None, tools_map=None, redis_client=None):
    return get_workflow(redis_client)


async def run_workflow(
    question: str,
    user_id: str,
    tools_map: dict[str, Any],
    redis_client=None,
    request_id: str = "",
    conversation_id: str = "",
) -> dict[str, Any]:
    """
    Ejecuta el workflow completo para un request.

    Args:
        question:        Pregunta del usuario.
        user_id:         Identificador del usuario.
        tools_map:       Dict de herramientas disponibles para el executor.
        redis_client:    Cliente Redis para feedback del supervisor.
        request_id:      UUID del request (tracing).
        conversation_id: ID de la conversación activa.

    Returns:
        AgentState final con final_answer, supervisor_score, etc.
    """
    from orchestration.state import initial_state

    graph = get_workflow(redis_client)
    state = initial_state(
        question=question,
        user_id=user_id,
        tools_map=tools_map,
        request_id=request_id,
        conversation_id=conversation_id,
    )
    result = graph.invoke(state)
    return result
