"""
SupervisorAgent — evalúa la calidad de la respuesta final.

Migrado desde backend-ia/agents/supervisor.py.
Nodo LangGraph: supervisor_node()
BaseAgent wrapper: SupervisorAgent
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from agents.supervisor.quality_scorer import evaluate
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.supervisor")


# ── Nodo LangGraph ─────────────────────────────────────────────────────────────

def supervisor_node(state: Dict[str, Any], redis_client=None) -> Dict[str, Any]:
    """Nodo LangGraph: evalúa la respuesta y decide ACCEPT o REPLAN."""
    return evaluate(state, redis_client=redis_client)


# ── BaseAgent wrapper ──────────────────────────────────────────────────────────

@AgentRegistry.register
class RemielAgent(BaseAgent):
    """
    Remiel — Supervisor Agent: evalúa la calidad de la respuesta final
    y decide si aceptarla o solicitar un re-plan.
    """

    name = "remiel"
    role = "Evaluar calidad de respuestas y controlar el ciclo de re-plan"
    version = "2.0.0"
    capabilities = ["quality_evaluation", "replan_decision", "feedback_recording"]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        state = task.get("state", {})
        redis_client = task.get("redis_client")
        result = evaluate(state, redis_client=redis_client)
        return AgentResult(
            success=True,
            output=result,
            agent_name=self.name,
            metadata={
                "decision": result.get("supervisor_decision"),
                "score": result.get("supervisor_score"),
            },
        )
