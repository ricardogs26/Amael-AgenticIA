"""
Agrupa el plan plano en batches para ejecución paralela.

Regla:
  - Pasos consecutivos no-REASONING → un batch paralelo
  - Pasos REASONING → siempre solos (sintetizan el contexto acumulado)

Migrado desde backend-ia/agents/grouper.py sin cambios de comportamiento.
"""
import logging
from typing import List

logger = logging.getLogger("agents.planner.grouper")


def group_plan_into_batches(plan: List[str]) -> List[List[str]]:
    """
    Convierte el plan plano en batches para ejecución en el workflow.

    Ejemplos:
      ["K8S_TOOL: A", "RAG_RETRIEVAL: B", "REASONING: C"]
      → [["K8S_TOOL: A", "RAG_RETRIEVAL: B"], ["REASONING: C"]]

      ["K8S_TOOL: A", "REASONING: B", "K8S_TOOL: C", "REASONING: D"]
      → [["K8S_TOOL: A"], ["REASONING: B"], ["K8S_TOOL: C"], ["REASONING: D"]]
    """
    batches: List[List[str]] = []
    tool_batch: List[str] = []

    for step in plan:
        if step.upper().startswith("REASONING:"):
            if tool_batch:
                batches.append(tool_batch)
                tool_batch = []
            batches.append([step])
        else:
            tool_batch.append(step)

    if tool_batch:
        batches.append(tool_batch)

    parallel_steps = sum(len(b) for b in batches if len(b) > 1)
    if parallel_steps:
        logger.info(
            f"[GROUPER] {len(batches)} grupos | "
            f"{parallel_steps}/{len(plan)} pasos en paralelo"
        )
    else:
        logger.info(f"[GROUPER] {len(batches)} grupos (todos secuenciales)")

    return batches
