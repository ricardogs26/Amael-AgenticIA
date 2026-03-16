"""
AgentState — estado compartido del workflow LangGraph.

Migrado desde backend-ia/agents/state.py y extendido con
campos de routing para el nuevo OrchestratorAgent.

El estado es un TypedDict que fluye a través del grafo:
  planner → grouper → batch_executor (loop) → supervisor
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict):
    """
    Estado completo del workflow de agentes.

    Campos del pipeline original (preservados):
      question         — pregunta del usuario
      plan             — lista de pasos ["STEP_TYPE: descripción", ...]
      batches          — plan agrupado en batches de ejecución paralela
      current_batch    — índice del batch siendo procesado
      current_step     — total de pasos ejecutados (para métricas)
      context          — resultados RAG acumulados
      tool_results     — resultados de herramientas como lista de dicts
      final_answer     — respuesta final al usuario
      user_id          — identificador del usuario
      retry_count      — re-plans disparados por supervisor (máx 1)
      supervisor_score — score de calidad 0-10 asignado por el supervisor
      supervisor_reason — justificación del supervisor
      supervisor_decision — "ACCEPT" | "REPLAN"
      tools_map        — {tool_name: callable} inyectado por request (grafo cacheable)

    Campos extendidos para la nueva arquitectura:
      request_id       — UUID del request (para correlación/tracing)
      conversation_id  — ID de conversación activa
      routing_intent   — intent detectado por el router ("kubernetes", "sre", etc.)
      agents_invoked   — agentes que participaron en resolver la request
    """

    # ── Pipeline original ─────────────────────────────────────────────────────
    question: str
    plan: List[str]
    batches: List[List[str]]
    current_batch: int
    current_step: int
    context: str
    tool_results: List[Dict[str, Any]]
    final_answer: Optional[str]
    user_id: str

    # ── Supervisor (P3) ───────────────────────────────────────────────────────
    retry_count: int
    supervisor_score: int
    supervisor_reason: str
    supervisor_decision: str       # "ACCEPT" | "REPLAN"

    # ── Tools inyectadas por request (P5-2) ───────────────────────────────────
    tools_map: Dict[str, Any]

    # ── Campos extendidos (nueva arquitectura) ────────────────────────────────
    request_id: str
    conversation_id: str
    routing_intent: str            # intent del AgentRouter
    agents_invoked: List[str]      # agentes que participaron


def initial_state(
    question: str,
    user_id: str,
    tools_map: Dict[str, Any],
    request_id: str = "",
    conversation_id: str = "",
) -> AgentState:
    """
    Crea un AgentState inicial con valores por defecto.

    Usar en el punto de entrada del workflow (OrchestratorAgent)
    para garantizar que todos los campos estén inicializados.
    """
    return AgentState(
        question=question,
        plan=[],
        batches=[],
        current_batch=0,
        current_step=0,
        context="",
        tool_results=[],
        final_answer=None,
        user_id=user_id,
        retry_count=0,
        supervisor_score=0,
        supervisor_reason="",
        supervisor_decision="",
        tools_map=tools_map,
        request_id=request_id,
        conversation_id=conversation_id,
        routing_intent="",
        agents_invoked=[],
    )
