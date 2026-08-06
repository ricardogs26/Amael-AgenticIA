"""
Router /api — endpoints de observabilidad.

Endpoints:
  GET /api/slo/status          — estado actual de todos los SLOs con datos de Prometheus
  GET /api/agents              — lista de agentes registrados en AgentRegistry
  GET /api/health              — estado completo de todos los componentes (API-friendly)
  GET /api/health/{component}  — granular: postgres | redis | qdrant | ollama | k8s_agent
  GET /api/llm/usage           — tokens consumidos y costo estimado por modelo y agente
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


@router.get("/llm/usage")
async def get_llm_usage(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Resumen de tokens consumidos y costo estimado en USD desde el último reinicio.

    Lee directamente los contadores Prometheus en memoria — sin latencia de red.
    Para datos históricos usa Grafana: amael_llm_tokens_total, amael_llm_cost_usd_total.

    Returns:
        {
          "total": { "input_tokens": N, "output_tokens": N, "total_tokens": N, "cost_usd": X },
          "by_model": { "qwen2.5:14b": { "input": N, "output": N, "cost_usd": X }, ... },
          "by_agent": { "planner": { "input": N, "output": N, "cost_usd": X }, ... },
          "by_model_agent": { "qwen2.5:14b|planner": { ... }, ... },
          "calls_by_agent": { "planner": N, ... },
          "price_table": { "qwen2.5:14b": {"input_per_m": 0.0, "output_per_m": 0.0}, ... }
        }
    """
    from prometheus_client import REGISTRY  # type: ignore
    from agents.base.llm_factory import _COST_PER_MILLION

    # Leer contadores directamente del registro Prometheus en memoria
    def _read_counter(metric_name: str) -> list[tuple[dict, float]]:
        """Retorna [(labels_dict, value)] para todos los samples del counter."""
        samples = []
        try:
            for metric in REGISTRY.collect():
                if metric.name == metric_name:
                    for sample in metric.samples:
                        if sample.name == metric_name + "_total":
                            samples.append((sample.labels, sample.value))
        except Exception:
            pass
        return samples

    tokens_samples = _read_counter("amael_llm_tokens")
    cost_samples   = _read_counter("amael_llm_cost_usd")
    calls_samples  = _read_counter("amael_llm_calls")

    # Agregar por modelo, agente y combinación
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    by_model_agent: dict[str, dict] = {}
    total_input = total_output = 0

    for labels, value in tokens_samples:
        model      = labels.get("model", "unknown")
        agent      = labels.get("agent", "other")
        token_type = labels.get("token_type", "input")
        val        = int(value)

        key = f"{model}|{agent}"
        for d in (by_model.setdefault(model, {}), by_agent.setdefault(agent, {}), by_model_agent.setdefault(key, {})):
            d[token_type] = d.get(token_type, 0) + val

        if token_type == "input":
            total_input += val
        else:
            total_output += val

    # Costo total por modelo/agente
    total_cost = 0.0
    for labels, value in cost_samples:
        model = labels.get("model", "unknown")
        agent = labels.get("agent", "other")
        key   = f"{model}|{agent}"
        by_model.setdefault(model, {})["cost_usd"] = by_model[model].get("cost_usd", 0.0) + value
        by_agent.setdefault(agent, {})["cost_usd"] = by_agent[agent].get("cost_usd", 0.0) + value
        by_model_agent.setdefault(key, {})["cost_usd"] = by_model_agent[key].get("cost_usd", 0.0) + value
        total_cost += value

    # Llamadas por agente
    calls_by_agent: dict[str, int] = {}
    for labels, value in calls_samples:
        agent = labels.get("agent", "other")
        calls_by_agent[agent] = calls_by_agent.get(agent, 0) + int(value)

    # Tabla de precios activa
    price_table = {
        model: {"input_per_m": costs[0], "output_per_m": costs[1]}
        for model, costs in _COST_PER_MILLION.items()
    }

    return {
        "total": {
            "input_tokens":  total_input,
            "output_tokens": total_output,
            "total_tokens":  total_input + total_output,
            "cost_usd":      round(total_cost, 6),
        },
        "by_model":       {k: {t: int(v) if t != "cost_usd" else round(v, 6) for t, v in d.items()} for k, d in by_model.items()},
        "by_agent":       {k: {t: int(v) if t != "cost_usd" else round(v, 6) for t, v in d.items()} for k, d in by_agent.items()},
        "by_model_agent": {k: {t: int(v) if t != "cost_usd" else round(v, 6) for t, v in d.items()} for k, d in by_model_agent.items()},
        "calls_by_agent": calls_by_agent,
        "price_table":    price_table,
    }


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
