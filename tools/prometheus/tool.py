"""
PrometheusTool — consultas PromQL contra Prometheus.

Capacidades:
  query(promql)                         — instant query
  query_range(promql, start, end, step) — range query para gráficas
  query_aliased(alias)                  — alias predefinidos para queries frecuentes

Migrado desde k8s-agent/main.py → query_prometheus() + _PROMETHEUS_ALIASES.
"""
from __future__ import annotations

import logging
import os

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.prometheus")

_PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090",
)

# Aliases para queries frecuentes — los mismos del k8s-agent original
_PROMETHEUS_ALIASES: dict[str, str] = {
    "cpu_pods": (
        'sum(rate(container_cpu_usage_seconds_total'
        '{namespace="amael-ia",container!=""}[5m])) by (pod)'
    ),
    "ram_pods": (
        'sum(container_memory_working_set_bytes'
        '{namespace="amael-ia",container!=""}) by (pod) / 1024 / 1024'
    ),
    "error_rate": (
        'sum(rate(http_requests_total{namespace="amael-ia",status=~"5.."}[5m]))'
        ' / sum(rate(http_requests_total{namespace="amael-ia"}[5m]))'
    ),
    "request_rate": (
        'sum(rate(http_requests_total{namespace="amael-ia"}[5m])) by (handler)'
    ),
    "llm_latency": (
        'histogram_quantile(0.90, rate(amael_llm_request_duration_seconds_bucket[5m]))'
    ),
    "sre_loop": (
        'rate(amael_sre_loop_runs_total[5m]) by (result)'
    ),
    "agent_latency": (
        'histogram_quantile(0.99, rate(amael_executor_step_latency_seconds_bucket[5m]))'
        ' by (step_type)'
    ),
    "disk_node": (
        '(1 - node_filesystem_avail_bytes{mountpoint="/"}'
        ' / node_filesystem_size_bytes{mountpoint="/"}) * 100'
    ),
}


# ── Inputs ────────────────────────────────────────────────────────────────────

class QueryInput(ToolInput):
    promql: str
    limit: int = 15

class QueryRangeInput(ToolInput):
    promql: str
    start: str   # RFC3339 o Unix timestamp
    end: str
    step: str = "60s"
    limit: int = 100

class QueryAliasedInput(ToolInput):
    alias: str
    limit: int = 15


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class PrometheusTool(BaseTool):
    """
    Integración con Prometheus: instant queries, range queries y aliases.
    Usada por SREAgent y el pipeline LangGraph para resolver steps MONITORING.
    """

    name            = "prometheus"
    description     = "PromQL queries contra Prometheus: instant, range y aliases predefinidos"
    version         = "1.0.0"
    external_system = "prometheus"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, QueryInput):
            return await self.query(input)
        if isinstance(input, QueryRangeInput):
            return await self.query_range(input)
        if isinstance(input, QueryAliasedInput):
            return await self.query_aliased(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def query(self, input: QueryInput) -> ToolOutput:
        """Ejecuta una instant query PromQL."""
        promql = input.promql.strip("'\" \n")
        try:
            resp = _req.get(
                f"{_PROMETHEUS_URL}/api/v1/query",
                params={"query": promql},
                timeout=10,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"Prometheus HTTP {resp.status_code}",
                    source=self.name,
                )
            data = resp.json()
            if data.get("status") != "success":
                return ToolOutput.fail(
                    f"Prometheus error: {data.get('error', 'unknown')}",
                    source=self.name,
                )
            results = data["data"]["result"][: input.limit]
            simplified = [
                {
                    "metric": r.get("metric", {}),
                    "value":  r.get("value", [None, None])[1],
                }
                for r in results
            ]
            return ToolOutput.ok(
                data=simplified,
                source=self.name,
                count=len(simplified),
                query=promql,
            )
        except Exception as exc:
            logger.error(f"[prometheus_tool] query error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def query_range(self, input: QueryRangeInput) -> ToolOutput:
        """Ejecuta una range query PromQL para datos temporales."""
        promql = input.promql.strip("'\" \n")
        try:
            resp = _req.get(
                f"{_PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": promql,
                    "start": input.start,
                    "end":   input.end,
                    "step":  input.step,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"Prometheus HTTP {resp.status_code}",
                    source=self.name,
                )
            data    = resp.json()
            results = data.get("data", {}).get("result", [])[: input.limit]
            return ToolOutput.ok(
                data=results,
                source=self.name,
                count=len(results),
                query=promql,
            )
        except Exception as exc:
            logger.error(f"[prometheus_tool] query_range error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def query_aliased(self, input: QueryAliasedInput) -> ToolOutput:
        """
        Ejecuta una query por alias predefinido.
        Retorna la lista de aliases disponibles si el alias no existe.
        """
        alias  = input.alias.lower().strip()
        promql = _PROMETHEUS_ALIASES.get(alias)
        if not promql:
            available = list(_PROMETHEUS_ALIASES.keys())
            return ToolOutput.fail(
                f"Alias '{alias}' no existe. Disponibles: {available}",
                source=self.name,
                available_aliases=available,
            )
        return await self.query(QueryInput(promql=promql, limit=input.limit))

    async def health_check(self) -> bool:
        """Verifica que Prometheus responde con una query trivial."""
        try:
            resp = _req.get(
                f"{_PROMETHEUS_URL}/api/v1/query",
                params={"query": "1"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning(f"[prometheus_tool] health_check falló: {exc}")
            return False
