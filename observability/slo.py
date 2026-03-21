"""
SLO/SLI definitions para Amael-AgenticIA.

Define targets de disponibilidad y latencia para los endpoints críticos.
El endpoint GET /api/slo/status consulta Prometheus en tiempo real
para calcular el burn rate del error budget en la ventana de 24h.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("observability.slo")

# ── SLO targets ───────────────────────────────────────────────────────────────

@dataclass
class SLOTarget:
    name: str
    handler: str          # label del handler en Prometheus
    availability: float   # 0.0–1.0 (ej. 0.995 = 99.5%)
    latency_p99_ms: float # milisegundos
    window_hours: int = 24


SLO_TARGETS: list[SLOTarget] = [
    SLOTarget(
        name="chat",
        handler="/api/chat",
        availability=0.995,
        latency_p99_ms=30_000,  # 30s (pipeline LangGraph completo)
        window_hours=24,
    ),
    SLOTarget(
        name="planner_daily",
        handler="/api/planner/daily",
        availability=0.990,
        latency_p99_ms=60_000,  # 60s (genera plan + WhatsApp)
        window_hours=24,
    ),
    SLOTarget(
        name="ingest",
        handler="/api/ingest",
        availability=0.990,
        latency_p99_ms=120_000,  # 120s (PDF chunking + embeddings)
        window_hours=24,
    ),
]


# ── Prometheus queries ────────────────────────────────────────────────────────

def _prometheus_url() -> str:
    try:
        from config.settings import settings
        return getattr(settings, "prometheus_url", "http://prometheus-server.observability:80")
    except Exception:
        return "http://prometheus-server.observability:80"


def _query(promql: str, timeout: float = 5.0) -> float | None:
    """Ejecuta una query instantánea en Prometheus. Retorna el primer valor escalar."""
    try:
        import httpx
        url = f"{_prometheus_url()}/api/v1/query"
        resp = httpx.get(url, params={"query": promql}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        logger.debug(f"[slo] Prometheus query failed: {exc}")
    return None


def _availability(handler: str, window_hours: int) -> float | None:
    """
    Calcula disponibilidad real basada en métricas HTTP.
    success_rate = (total - 5xx) / total
    """
    window = f"{window_hours}h"
    total_q = (
        f'sum(increase(amael_http_requests_total{{handler="{handler}"}}[{window}]))'
    )
    error_q = (
        f'sum(increase(amael_http_requests_total{{handler="{handler}",'
        f'status_code=~"5.."}}[{window}]))'
    )
    total = _query(total_q)
    errors = _query(error_q)
    if total is None or total == 0:
        return None
    error_rate = (errors or 0) / total
    return 1.0 - error_rate


def _latency_p99(handler: str, window_hours: str = "1h") -> float | None:
    """Retorna p99 de latencia en ms para el handler dado."""
    p99_q = (
        f'histogram_quantile(0.99, rate(amael_http_request_latency_seconds_bucket'
        f'{{handler="{handler}"}}[{window_hours}])) * 1000'
    )
    return _query(p99_q)


def _error_budget_remaining(availability_actual: float, target: float, window_hours: int) -> float:
    """
    Retorna % del error budget restante (0–100).
    budget_consumed = (1 - availability_actual) / (1 - target)
    """
    allowed_error = 1.0 - target
    if allowed_error <= 0:
        return 0.0
    actual_error = 1.0 - availability_actual
    consumed = actual_error / allowed_error
    remaining = max(0.0, (1.0 - consumed) * 100.0)
    return round(remaining, 2)


# ── Public API ────────────────────────────────────────────────────────────────

def get_slo_status() -> list[dict[str, Any]]:
    """
    Retorna el estado actual de todos los SLOs con datos en tiempo real de Prometheus.
    Usado por GET /api/slo/status.
    """
    results = []
    for slo in SLO_TARGETS:
        availability = _availability(slo.handler, slo.window_hours)
        latency_p99 = _latency_p99(slo.handler)

        if availability is not None:
            budget_remaining = _error_budget_remaining(
                availability, slo.availability, slo.window_hours
            )
            meets_availability = availability >= slo.availability
        else:
            budget_remaining = None
            meets_availability = None

        meets_latency = (
            latency_p99 <= slo.latency_p99_ms
            if latency_p99 is not None
            else None
        )

        status = "ok"
        if meets_availability is False or meets_latency is False:
            status = "breached"
        elif budget_remaining is not None and budget_remaining < 10:
            status = "at_risk"
        elif availability is None:
            status = "no_data"

        results.append({
            "name": slo.name,
            "handler": slo.handler,
            "status": status,
            "target": {
                "availability": slo.availability,
                "latency_p99_ms": slo.latency_p99_ms,
                "window_hours": slo.window_hours,
            },
            "actual": {
                "availability": round(availability, 6) if availability is not None else None,
                "latency_p99_ms": round(latency_p99, 1) if latency_p99 is not None else None,
            },
            "error_budget_remaining_pct": budget_remaining,
            "meets_availability": meets_availability,
            "meets_latency": meets_latency,
        })
    return results
