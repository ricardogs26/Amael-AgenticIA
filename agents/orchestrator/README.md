# Orchestrator Agent — Nota de Arquitectura

**Estado**: Módulo vacío — lógica real en `orchestration/`

## Por qué está vacío

La lógica de orquestación vive en el módulo `orchestration/` (no en `agents/orchestrator/`):

| Archivo | Rol |
|---------|-----|
| `orchestration/workflow_engine.py` | Compila y cachea el grafo LangGraph |
| `orchestration/agent_router.py` | Detecta intent y decide qué agentes invocar |
| `orchestration/agent_dispatcher.py` | Ejecuta el workflow o fast-paths |
| `orchestration/context_factory.py` | Construye `AgentContext` con dependencias inyectadas |
| `orchestration/state.py` | `AgentState` TypedDict — fluye a través del grafo |

## Flujo de orquestación actual

```
POST /api/chat
    └── AgentRouter.route(question)      → detecta intent
    └── ToolRegistry.names()             → obtiene tools disponibles
    └── dispatch(question, tools_map)    → ejecuta workflow LangGraph
        └── get_workflow()               → grafo compilado (cacheado)
            └── planner → grouper → batch_executor → supervisor
```

## Si en el futuro se necesita un OrchestratorAgent standalone

Implementar aquí una clase `OrchestratorAgent(BaseAgent)` que envuelva
`orchestration/agent_dispatcher.dispatch()` para poder invocarlo como agente
desde otros contextos (CLI, SDK, tests).
