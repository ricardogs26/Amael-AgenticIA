"""
ExecutorAgent — ejecuta los batches del plan generado por el PlannerAgent.

Migrado desde backend-ia/agents/executor.py.
Nodo LangGraph: batch_executor_node()
BaseAgent wrapper: ExecutorAgent
"""
from __future__ import annotations

import logging
import time
from typing import Any

from agents.base.agent_registry import AgentRegistry
from agents.executor.batch_runner import (
    MAX_CONTEXT_CHARS,
    _step_type,
    _truncate,
    run_parallel_batch,
    run_reasoning_step,
    run_tool_step,
)
from core.agent_base import AgentResult, BaseAgent
from observability.metrics import EXECUTOR_PARALLEL_BATCH_SIZE
from observability.tracing import tracer

logger = logging.getLogger("agents.executor")


# ── Nodo LangGraph ─────────────────────────────────────────────────────────────

def batch_executor_node(
    state: dict[str, Any],
    llm=None,
    tools_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Nodo LangGraph: ejecuta un batch del plan y retorna el state actualizado.

    Lee tools_map desde state cuando no se pasa explícitamente (P5-2).
    Esto permite que el grafo LangGraph sea cacheado a nivel de proceso.
    """
    if tools_map is None:
        tools_map = state.get("tools_map", {})

    batches = state.get("batches", [])
    current_batch_idx = state.get("current_batch", 0)
    current_step = state.get("current_step", 0)

    if current_batch_idx >= len(batches):
        return {"current_batch": current_batch_idx}

    batch = batches[current_batch_idx]
    logger.info(
        f"[executor] Batch {current_batch_idx + 1}/{len(batches)} "
        f"({len(batch)} paso{'s' if len(batch) > 1 else ''}): {batch}"
    )

    t0 = time.time()
    EXECUTOR_PARALLEL_BATCH_SIZE.observe(len(batch))

    with tracer.start_as_current_span("agent.executor.batch") as span:
        span.set_attribute("agent.batch_index", current_batch_idx)
        span.set_attribute("agent.batch_size", len(batch))
        span.set_attribute("agent.batch", str(batch))

        # ── REASONING (siempre paso único) ────────────────────────────────────
        if len(batch) == 1 and batch[0].upper().startswith("REASONING:"):
            step = batch[0]
            t_step = time.time()
            with tracer.start_as_current_span("agent.executor.reasoning") as r_span:
                r_span.set_attribute("agent.step", step)
                new_answer, new_context = run_reasoning_step(step, state)
                from observability.metrics import (
                    EXECUTOR_STEP_LATENCY_SECONDS,
                    EXECUTOR_STEPS_TOTAL,
                )
                elapsed = time.time() - t_step
                EXECUTOR_STEP_LATENCY_SECONDS.labels(step_type="REASONING").observe(elapsed)
                EXECUTOR_STEPS_TOTAL.labels(step_type="REASONING").inc()
                r_span.set_attribute("agent.step_latency_seconds", elapsed)

        # ── Paso único de herramienta ─────────────────────────────────────────
        elif len(batch) == 1:
            result = run_tool_step(batch[0], state, tools_map)
            stype = _step_type(batch[0])
            new_context = state.get("context", "") or ""
            if stype == "RAG_RETRIEVAL":
                combined = (new_context + "\n" + result).strip() if new_context else result
                new_context = _truncate(combined, MAX_CONTEXT_CHARS, "rag_context")
            new_answer = result

        # ── Batch paralelo ────────────────────────────────────────────────────
        else:
            new_answer, new_context = run_parallel_batch(batch, state, tools_map)

        span.set_attribute("agent.batch_latency_seconds", time.time() - t0)

    return {
        "final_answer": new_answer,
        "context": new_context,
        "current_batch": current_batch_idx + 1,
        "current_step": current_step + len(batch),
    }


# ── BaseAgent wrapper ──────────────────────────────────────────────────────────

@AgentRegistry.register
class ExecutorAgent(BaseAgent):
    """
    Agente ejecutor: procesa un batch de pasos del plan usando las tools disponibles.
    """

    name = "executor"
    role = "Ejecutar pasos del plan usando herramientas especializadas"
    version = "2.0.0"
    capabilities = [
        "k8s_query", "rag_retrieval", "web_search",
        "productivity_tools", "document_generation", "llm_reasoning",
        "parallel_execution",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        state = task.get("state", {})
        tools_map = task.get("tools_map", state.get("tools_map", {}))
        result = batch_executor_node(state, tools_map=tools_map)
        return AgentResult(
            success=True,
            output=result,
            agent_name=self.name,
        )
