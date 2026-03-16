"""
Handlers de ejecución para cada StepType del plan.

Cada handler recibe (query, state, tools_map) y retorna un string con el resultado.
Migrado desde backend-ia/agents/executor.py, separado por responsabilidad.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from observability.metrics import EXECUTOR_ERRORS_TOTAL

logger = logging.getLogger("agents.executor.handlers")


def _user_can_use_k8s(user_id: str) -> bool:
    """Verifica que el usuario tiene role='admin' en user_profile."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_profile WHERE user_id = %s AND role = 'admin' AND status = 'active'",
                    (user_id,),
                )
                if cur.fetchone():
                    return True
                # También buscar por identidad (número WhatsApp → canonical_user_id admin)
                cur.execute(
                    """
                    SELECT 1 FROM user_identities ui
                    JOIN user_profile up ON up.user_id = ui.canonical_user_id
                    WHERE ui.identity_value = %s AND up.role = 'admin' AND up.status = 'active'
                    """,
                    (user_id,),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        logger.error(f"[executor] DB check K8s para {user_id!r}: {exc}")
        return False


def handle_k8s_tool(query: str, state: Dict[str, Any], tools_map: Dict[str, Any]) -> str:
    """Ejecuta una consulta K8s/infraestructura."""
    user_id = state.get("user_id", "unknown")

    if not _user_can_use_k8s(user_id):
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
