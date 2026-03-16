"""
Handlers de ejecución para cada StepType del plan.

Cada handler recibe (query, state, tools_map) y retorna un string con el resultado.
Migrado desde backend-ia/agents/executor.py, separado por responsabilidad.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

from observability.metrics import EXECUTOR_ERRORS_TOTAL

logger = logging.getLogger("agents.executor.handlers")


def _get_k8s_allowed_users() -> list[str]:
    """Lee la whitelist de K8s desde settings (lazy para evitar import circular)."""
    try:
        from config.settings import settings
        return settings.k8s_allowed_users
    except Exception:
        csv = os.environ.get("K8S_ALLOWED_USERS_CSV", "")
        return [u.strip() for u in csv.split(",") if u.strip()]


def handle_k8s_tool(query: str, state: Dict[str, Any], tools_map: Dict[str, Any]) -> str:
    """Ejecuta una consulta K8s/infraestructura."""
    k8s_allowed = _get_k8s_allowed_users()
    user_id = state.get("user_id", "unknown")

    if k8s_allowed and user_id not in k8s_allowed:
        logger.warning(f"[executor] K8S_TOOL bloqueado para user={user_id}")
        return "Lo siento, tu usuario no cuenta con los privilegios de administrador requeridos."

    k8s_func = tools_map.get("k8s")
    if not k8s_func:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="K8S_TOOL").inc()
        return "Error: Herramienta K8s no disponible."

    return k8s_func(query)


def handle_rag_retrieval(query: str, state: Dict[str, Any], tools_map: Dict[str, Any]) -> str:
    """Ejecuta una búsqueda RAG en Qdrant."""
    rag_func = tools_map.get("rag")
    if not rag_func:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="RAG_RETRIEVAL").inc()
        return "Error: Herramienta RAG no disponible."
    return rag_func(query)


def handle_productivity_tool(
    query: str, state: Dict[str, Any], tools_map: Dict[str, Any]
) -> str:
    """Ejecuta una consulta de productividad (calendario/email)."""
    prod_func = tools_map.get("productivity")
    if not prod_func:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="PRODUCTIVITY_TOOL").inc()
        return "Error: Herramienta de productividad no disponible."
    return prod_func(query)


def handle_web_search(query: str, state: Dict[str, Any], tools_map: Dict[str, Any]) -> str:
    """Ejecuta una búsqueda web (DuckDuckGo)."""
    web_func = tools_map.get("web_search")
    if not web_func:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="WEB_SEARCH").inc()
        return "Error: Herramienta de búsqueda web no disponible."
    return web_func(query)


def handle_document_tool(
    query: str, state: Dict[str, Any], tools_map: Dict[str, Any]
) -> str:
    """Genera un documento formal (oficio, reporte, memorando)."""
    doc_func = tools_map.get("document")
    if not doc_func:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="DOCUMENT_TOOL").inc()
        return "Error: Herramienta de documentos no disponible."
    return doc_func(query)


# Mapa de tipo → handler
STEP_HANDLERS = {
    "K8S_TOOL":          handle_k8s_tool,
    "RAG_RETRIEVAL":     handle_rag_retrieval,
    "PRODUCTIVITY_TOOL": handle_productivity_tool,
    "WEB_SEARCH":        handle_web_search,
    "DOCUMENT_TOOL":     handle_document_tool,
}
