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


def handle_tts_tool(query: str, state: Dict[str, Any], tools_map: Dict[str, Any]) -> str:
    """
    Síntesis de voz y envío como nota de voz WhatsApp.

    El query puede ser solo texto, o con formato:
      "para <phone>: <texto>"   → envía al número indicado
      "<texto>"                 → envía al ADMIN_PHONE configurado
    """
    import asyncio
    import re

    tts_tool = tools_map.get("piper")
    if not tts_tool:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="TTS_TOOL").inc()
        return "Error: Herramienta TTS no disponible (cosyvoice-service no registrado)."

    # Parsear "para <phone>: <texto>"
    phone = None
    text  = query.strip()
    m = re.match(r"^para\s+(\d{10,15}):\s*(.+)$", text, re.DOTALL | re.IGNORECASE)
    if m:
        phone = m.group(1)
        text  = m.group(2).strip()

    # Determinar idioma desde el texto
    language = "es"
    en_markers = ("the ", "this ", "hello", "please", "generate", " is ", " are ")
    if any(mk in text.lower() for mk in en_markers):
        language = "en"

    try:
        from tools.piper.tool import SynthesizeAndSendInput
        result = asyncio.get_event_loop().run_until_complete(
            tts_tool.synthesize_and_send(
                SynthesizeAndSendInput(text=text, phone=phone)
            )
        )
        if result.success:
            d = result.data
            return (
                f"Nota de voz enviada a {d['phone']} "
                f"({d['duration_seconds']:.1f}s, {d['chars']} caracteres)."
            )
        return f"Error al enviar nota de voz: {result.error}"
    except Exception as exc:
        logger.error(f"[executor] TTS_TOOL error: {exc}")
        EXECUTOR_ERRORS_TOTAL.labels(step_type="TTS_TOOL").inc()
        return f"Error en síntesis de voz: {exc}"


def handle_code_generation(
    query: str, state: Dict[str, Any], tools_map: Dict[str, Any]
) -> str:
    """
    Ejecuta una tarea de generación/modificación de código usando GitHubTool (Gabriel).

    El query puede incluir instrucciones estructuradas como:
      "read <owner>/<repo>/<path>"
      "create_branch <owner>/<repo> branch=<name> from=<main>"
      "commit <owner>/<repo>/<path> branch=<name> message=<msg> content=<...>"
      "pull_request <owner>/<repo> head=<branch> title=<...> body=<...>"
    O puede ser texto libre que se resuelve mediante LLM en el paso REASONING siguiente.
    """
    import asyncio
    import re

    github_tool = tools_map.get("github")
    if not github_tool:
        EXECUTOR_ERRORS_TOTAL.labels(step_type="CODE_GENERATION").inc()
        return "Error: GitHubTool no disponible. Configura GITHUB_TOKEN en el backend."

    try:
        from tools.github.tool import (
            CreateBranchInput,
            CreateCommitInput,
            CreatePullRequestInput,
            GetFileContentsInput,
        )

        loop = asyncio.get_event_loop()
        q = query.strip()

        # ── read <owner>/<repo>/<path> [@ref] ────────────────────────────────
        m = re.match(
            r"^read\s+([^/]+)/([^/]+)/(.+?)(?:\s+@(\S+))?$", q, re.IGNORECASE
        )
        if m:
            result = loop.run_until_complete(
                github_tool.get_file_contents(
                    GetFileContentsInput(
                        owner=m.group(1),
                        repo=m.group(2),
                        path=m.group(3),
                        ref=m.group(4) or "main",
                    )
                )
            )
            if result.success:
                content = result.data.get("content", "")
                sha = result.data.get("sha", "")
                return f"[sha:{sha}]\n{content}"
            return f"Error leyendo archivo: {result.error}"

        # ── create_branch <owner>/<repo> branch=<name> [from=<ref>] ──────────
        m = re.match(
            r"^create_branch\s+([^/]+)/([^/]+)\s+branch=(\S+)(?:\s+from=(\S+))?$",
            q, re.IGNORECASE,
        )
        if m:
            result = loop.run_until_complete(
                github_tool.create_branch(
                    CreateBranchInput(
                        owner=m.group(1),
                        repo=m.group(2),
                        branch=m.group(3),
                        from_ref=m.group(4) or "main",
                    )
                )
            )
            if result.success:
                return (
                    f"Rama '{result.data['branch']}' creada desde "
                    f"'{result.data['from_ref']}' (sha: {result.data['sha'][:8]})."
                )
            return f"Error creando rama: {result.error}"

        # ── pull_request <owner>/<repo> head=<branch> title=<...> [body=<...>] ─
        m = re.match(
            r"^pull_request\s+([^/]+)/([^/]+)\s+head=(\S+)\s+title=(.+?)(?:\s+body=(.+))?$",
            q, re.IGNORECASE | re.DOTALL,
        )
        if m:
            result = loop.run_until_complete(
                github_tool.create_pull_request(
                    CreatePullRequestInput(
                        owner=m.group(1),
                        repo=m.group(2),
                        head=m.group(3),
                        title=m.group(4).strip(),
                        body=m.group(5).strip() if m.group(5) else "",
                    )
                )
            )
            if result.success:
                d = result.data
                return f"PR #{d['number']} creado: {d['url']}"
            return f"Error creando PR: {result.error}"

        # ── fallback: devolver el query para resolución LLM en REASONING ──────
        return f"[CODE_GENERATION] Instrucción recibida por Gabriel: {q}"

    except Exception as exc:
        logger.error(f"[executor] CODE_GENERATION error: {exc}")
        EXECUTOR_ERRORS_TOTAL.labels(step_type="CODE_GENERATION").inc()
        return f"Error en CODE_GENERATION: {exc}"


# Mapa de tipo → handler
STEP_HANDLERS = {
    "K8S_TOOL":          handle_k8s_tool,
    "RAG_RETRIEVAL":     handle_rag_retrieval,
    "PRODUCTIVITY_TOOL": handle_productivity_tool,
    "WEB_SEARCH":        handle_web_search,
    "DOCUMENT_TOOL":     handle_document_tool,
    "TTS_TOOL":          handle_tts_tool,
    "CODE_GENERATION":   handle_code_generation,
}
