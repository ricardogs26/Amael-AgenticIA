"""
tools — Integraciones con sistemas externos de Amael-AgenticIA.

Herramientas disponibles:
  prometheus  — PromQL queries (instant, range, aliases)
  grafana     — Dashboards, metadata y screenshots
  whatsapp    — Mensajes y alertas SRE
  github      — Repos, issues, PRs y workflow runs

Diferencia con Skills:
  - Skill: capacidad interna de la plataforma (K8s, RAG, LLM, Vault)
  - Tool:  integración con sistema externo (Prometheus, Grafana, GitHub)

Uso rápido:
    from tools.registry import ToolRegistry, register_all_tools

    register_all_tools()                        # llamar una vez en startup
    prom = ToolRegistry.get("prometheus")
    output = await prom.execute(QueryInput(promql="up"))

    # Health check de todas las tools
    status = await ToolRegistry.health_check_all()
"""
from tools.github.tool import GitHubTool
from tools.grafana.tool import GrafanaTool

# Tools individuales
from tools.prometheus.tool import PrometheusTool
from tools.registry import ToolNotFoundError, ToolRegistry, register_all_tools
from tools.whatsapp.tool import WhatsAppTool

__all__ = [
    # Registry
    "ToolRegistry",
    "ToolNotFoundError",
    "register_all_tools",
    # Tool classes
    "PrometheusTool",
    "GrafanaTool",
    "WhatsAppTool",
    "GitHubTool",
]
