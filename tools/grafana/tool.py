"""
GrafanaTool — consultas e imágenes de dashboards Grafana.

Capacidades:
  list_dashboards()              — lista los 8 dashboards Amael
  get_dashboard(uid)             — metadata + URL del dashboard
  screenshot(dashboard_key)      — captura PNG vía whatsapp-bridge /screenshot
  search_dashboards(query)       — búsqueda por nombre (Grafana API)

Migrado desde k8s-agent/main.py → capture_grafana_screenshot() + list_grafana_dashboards().
"""
from __future__ import annotations

import logging
import os

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.grafana")

_GRAFANA_URL = os.environ.get(
    "GRAFANA_URL",
    "http://kube-prometheus-stack-grafana.observability.svc.cluster.local",
)
_GRAFANA_USER     = os.environ.get("GRAFANA_USER", "admin")
_GRAFANA_PASSWORD = os.environ.get("GRAFANA_PASSWORD", "")

# URL del whatsapp-bridge para capturas Puppeteer
_WA_BRIDGE_URL = os.environ.get(
    "WHATSAPP_BRIDGE_URL",
    "http://whatsapp-bridge-service.amael-ia.svc.cluster.local:3000",
)

# ── Mapa de dashboards Amael ──────────────────────────────────────────────────
# Keyed by alias amigable → uid (Grafana) + título
_AMAEL_DASHBOARDS: dict[str, dict[str, str]] = {
    "llm":        {"uid": "amael-llm",        "title": "LLM & HTTP"},
    "agent":      {"uid": "amael-agent",      "title": "Pipeline de Agente"},
    "rag":        {"uid": "amael-rag",        "title": "RAG Performance"},
    "infra":      {"uid": "amael-infra",      "title": "Infraestructura & GPU"},
    "supervisor": {"uid": "amael-supervisor", "title": "Supervisor & Calidad"},
    "security":   {"uid": "amael-security",   "title": "Seguridad & Rate Limiting"},
    "service_map":{"uid": "amael-service-map","title": "Service Map"},
    "sre":        {"uid": "amael-sre-agent",  "title": "SRE Autónomo"},
}


# ── Inputs ────────────────────────────────────────────────────────────────────

class ListDashboardsInput(ToolInput):
    pass

class GetDashboardInput(ToolInput):
    uid: str            # UID de Grafana o alias amigable

class ScreenshotInput(ToolInput):
    dashboard_key: str  # Alias del mapa _AMAEL_DASHBOARDS
    width:  int = 1280
    height: int = 720

class SearchDashboardsInput(ToolInput):
    query: str
    limit: int = 10


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class GrafanaTool(BaseTool):
    """
    Integración con Grafana: lista dashboards, obtiene metadata y captura screenshots.
    Usada por SREAgent para adjuntar imágenes de métricas en alertas WhatsApp.
    """

    name            = "grafana"
    description     = "Dashboards Grafana: lista, metadata y screenshots de métricas"
    version         = "1.0.0"
    external_system = "grafana"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, ListDashboardsInput):
            return await self.list_dashboards(input)
        if isinstance(input, GetDashboardInput):
            return await self.get_dashboard(input)
        if isinstance(input, ScreenshotInput):
            return await self.screenshot(input)
        if isinstance(input, SearchDashboardsInput):
            return await self.search_dashboards(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def list_dashboards(self, input: ListDashboardsInput) -> ToolOutput:
        """Lista los 8 dashboards predefinidos de Amael con sus URLs."""
        result = []
        for alias, info in _AMAEL_DASHBOARDS.items():
            result.append({
                "alias": alias,
                "uid":   info["uid"],
                "title": info["title"],
                "url":   f"{_GRAFANA_URL}/d/{info['uid']}",
            })
        return ToolOutput.ok(
            data=result,
            source=self.name,
            count=len(result),
        )

    async def get_dashboard(self, input: GetDashboardInput) -> ToolOutput:
        """Obtiene metadata de un dashboard por UID o alias amigable."""
        # Resolver alias amigable
        uid = input.uid.lower().strip()
        if uid in _AMAEL_DASHBOARDS:
            info = _AMAEL_DASHBOARDS[uid]
            return ToolOutput.ok(
                data={
                    "uid":   info["uid"],
                    "title": info["title"],
                    "alias": uid,
                    "url":   f"{_GRAFANA_URL}/d/{info['uid']}",
                },
                source=self.name,
            )

        # UID real → consultar API Grafana
        try:
            resp = _req.get(
                f"{_GRAFANA_URL}/api/dashboards/uid/{uid}",
                auth=(_GRAFANA_USER, _GRAFANA_PASSWORD),
                timeout=10,
            )
            if resp.status_code == 404:
                available = list(_AMAEL_DASHBOARDS.keys())
                return ToolOutput.fail(
                    f"Dashboard '{uid}' no encontrado. Aliases disponibles: {available}",
                    source=self.name,
                    available_aliases=available,
                )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"Grafana HTTP {resp.status_code}",
                    source=self.name,
                )
            data = resp.json()
            meta = data.get("meta", {})
            dash = data.get("dashboard", {})
            return ToolOutput.ok(
                data={
                    "uid":   dash.get("uid", uid),
                    "title": dash.get("title", ""),
                    "url":   f"{_GRAFANA_URL}{meta.get('url', '')}",
                    "tags":  dash.get("tags", []),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[grafana_tool] get_dashboard error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def screenshot(self, input: ScreenshotInput) -> ToolOutput:
        """
        Captura un screenshot del dashboard vía whatsapp-bridge /screenshot (Puppeteer).

        Returns:
            ToolOutput con data = {"base64": "...", "url": "..."}
        """
        key = input.dashboard_key.lower().strip()
        if key not in _AMAEL_DASHBOARDS:
            available = list(_AMAEL_DASHBOARDS.keys())
            return ToolOutput.fail(
                f"Dashboard '{key}' no existe. Disponibles: {available}",
                source=self.name,
                available_aliases=available,
            )

        uid     = _AMAEL_DASHBOARDS[key]["uid"]
        dash_url = f"{_GRAFANA_URL}/d/{uid}?kiosk=true"

        try:
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/screenshot",
                json={
                    "url":    dash_url,
                    "width":  input.width,
                    "height": input.height,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"whatsapp-bridge HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            payload = resp.json()
            return ToolOutput.ok(
                data={
                    "base64": payload.get("image", ""),
                    "url":    dash_url,
                    "title":  _AMAEL_DASHBOARDS[key]["title"],
                },
                source=self.name,
                dashboard=key,
            )
        except Exception as exc:
            logger.error(f"[grafana_tool] screenshot error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def search_dashboards(self, input: SearchDashboardsInput) -> ToolOutput:
        """Busca dashboards por nombre usando la API de Grafana."""
        try:
            resp = _req.get(
                f"{_GRAFANA_URL}/api/search",
                params={"query": input.query, "type": "dash-db", "limit": input.limit},
                auth=(_GRAFANA_USER, _GRAFANA_PASSWORD),
                timeout=10,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"Grafana HTTP {resp.status_code}",
                    source=self.name,
                )
            results = resp.json()
            simplified = [
                {"uid": d.get("uid"), "title": d.get("title"), "url": f"{_GRAFANA_URL}{d.get('url','')}"}
                for d in results
            ]
            return ToolOutput.ok(
                data=simplified,
                source=self.name,
                count=len(simplified),
                query=input.query,
            )
        except Exception as exc:
            logger.error(f"[grafana_tool] search_dashboards error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def health_check(self) -> bool:
        """Verifica que Grafana responde en /api/health (non-blocking)."""
        import asyncio

        def _check() -> bool:
            try:
                resp = _req.get(
                    f"{_GRAFANA_URL}/api/health",
                    timeout=5,
                )
                return resp.status_code == 200
            except Exception as exc:
                logger.warning(f"[grafana_tool] health_check falló: {exc}")
                return False

        return await asyncio.to_thread(_check)
