"""
Router /api — endpoints de observabilidad.

Endpoints:
  GET /api/slo/status          — estado actual de todos los SLOs con datos de Prometheus
  GET /api/agents              — lista de agentes registrados en AgentRegistry
  GET /api/health              — estado completo de todos los componentes (API-friendly)
  GET /api/health/{component}  — granular: postgres | redis | qdrant | ollama | k8s_agent
  GET /api/graph               — grafo de agentes: topología del código + tráfico medido
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.observability")

router = APIRouter(prefix="/api", tags=["observability"])


@router.get("/slo/status")
async def get_slo_status(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Retorna el estado actual de los SLOs con datos en tiempo real de Prometheus.

    - status: ok | at_risk | breached | no_data
    - error_budget_remaining_pct: % del error budget restante en la ventana de 24h
    - meets_availability / meets_latency: null si no hay datos suficientes
    """
    from observability.slo import get_slo_status
    return {"slos": get_slo_status()}


@router.get("/health")
async def get_health(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Estado completo de todos los componentes de infraestructura.

    Equivalente a /ready pero accesible como API (siempre HTTP 200,
    el campo `status` indica ok | degraded | unavailable).
    """
    from observability.health import readiness
    result = await readiness()
    return result.model_dump()


@router.get("/health/{component}")
async def get_component_health(
    component: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Health check granular de un único componente.

    Componentes disponibles: postgres, redis, qdrant, ollama, k8s_agent
    """
    from observability.health import check_component
    result = await check_component(component)
    return result.model_dump()


@router.get("/agents")
async def list_agents(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Lista todos los agentes registrados en AgentRegistry con su metadata.

    Returns:
        { count: int, agents: [{ name, role, version, capabilities, ... }] }
    """
    from agents.base.agent_registry import AgentRegistry
    agents = AgentRegistry.list_agents()
    return {"count": len(agents), "agents": agents}


@router.get("/graph")
async def get_agent_graph(
    current_user: Annotated[dict, Depends(get_current_user)],
    traffic: bool = Query(True, description="Consultar Prometheus por el tráfico"),
    knowledge: bool = Query(True, description="Incluir runbooks y colecciones Qdrant"),
):
    """
    Grafo de agentes para renderizar en el front.

    La topología se deriva del código (grafo LangGraph + AgentRegistry +
    required_skills), así que un agente nuevo aparece solo. El tráfico sale del
    contador `amael_agent_edge_total`, acumulado desde el arranque del pod —
    sin ventana, por las razones documentadas en `agent_graph._apply_traffic`.
    La capa de conocimiento agrupa los runbooks de Qdrant por `issue_type` y
    lista las colecciones RAG con su dueño.

    Returns:
        { nodes: [...], edges: [...], metric, has_traffic, knowledge, counts }
    """
    from observability.agent_graph import build_graph
    return build_graph(include_traffic=traffic, include_knowledge=knowledge)
