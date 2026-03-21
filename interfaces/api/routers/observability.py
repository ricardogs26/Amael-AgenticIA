"""
Router /api — endpoints de observabilidad.

Endpoints:
  GET /api/slo/status          — estado actual de todos los SLOs con datos de Prometheus
  GET /api/agents              — lista de agentes registrados en AgentRegistry
  GET /api/health              — estado completo de todos los componentes (API-friendly)
  GET /api/health/{component}  — granular: postgres | redis | qdrant | ollama | k8s_agent
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

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
