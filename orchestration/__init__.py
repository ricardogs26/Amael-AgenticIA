"""
orchestration — Motor de orquestación de Amael-AgenticIA.

Componentes:
  state            — AgentState TypedDict + initial_state()
  workflow_engine  — LangGraph DAG (planner→grouper→executor→supervisor)
  agent_router     — Keyword matching + LLM fallback → RoutingDecision
  context_factory  — Construye AgentContext con skills inyectadas
  agent_dispatcher — Ejecuta: agente directo (SRE/Prod/Research) o LangGraph

Flujo completo de un request:
    1. AgentRouter.route(question)         → RoutingDecision
    2. AgentDispatcher.dispatch(...)       → result dict
       ├── intent directo (sre/prod/researcher) → AgentRegistry.get() + run()
       └── intent pipeline                     → LangGraph workflow

Uso rápido:
    from orchestration import AgentRouter, dispatch

    router   = AgentRouter()
    decision = await router.route("¿Estado del clúster?")
    result   = await dispatch(question, user_id, tools_map, decision)
    print(result["final_answer"])
"""
from orchestration.state import AgentState, initial_state
from orchestration.workflow_engine import (
    get_workflow,
    get_orchestrator,
    create_orchestrator,
    run_workflow,
)
from orchestration.agent_router import AgentRouter, RoutingDecision
from orchestration.context_factory import ContextFactory
from orchestration.agent_dispatcher import AgentDispatcher, dispatch

__all__ = [
    # State
    "AgentState",
    "initial_state",
    # Workflow
    "get_workflow",
    "get_orchestrator",
    "create_orchestrator",
    "run_workflow",
    # Router
    "AgentRouter",
    "RoutingDecision",
    # Context
    "ContextFactory",
    # Dispatcher
    "AgentDispatcher",
    "dispatch",
]
