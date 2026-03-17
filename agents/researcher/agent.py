"""
Research Agent — RAG sobre documentos de usuario + búsqueda web.

Responsabilidades:
  - RAG_RETRIEVAL: recupera documentos del usuario desde Qdrant (per-user collections)
  - WEB_SEARCH: busca información actualizada en DuckDuckGo / tipo de cambio
  - Ingesta de documentos (PDF, TXT, DOCX) → chunk → Qdrant

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("researcher", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.researcher.agent")


@AgentRegistry.register
class SandalphonAgent(BaseAgent):
    """
    Sandalphon — Research Agent: investigación y recuperación de información.

    Maneja dos tipos de step del Planner:
      - RAG_RETRIEVAL : similarity_search en Qdrant (colección del usuario)
      - WEB_SEARCH    : DuckDuckGo + fast-path tipo de cambio

    task dict esperado:
        {
            "step_type": "RAG_RETRIEVAL" | "WEB_SEARCH",
            "query": str,
            "user_email": str,   # requerido para RAG_RETRIEVAL
        }
    """

    name         = "sandalphon"
    role         = "Investigación: RAG sobre documentos de usuario + búsqueda web"
    version      = "1.0.0"
    capabilities = [
        "rag_retrieval",
        "web_search",
        "document_ingest",
        "currency_rates",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        step_type  = task.get("step_type", "").upper()
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error="query vacía",
            )

        if step_type == "RAG_RETRIEVAL":
            return await self._handle_rag(query, user_email)
        elif step_type == "WEB_SEARCH":
            return await self._handle_web(query)
        else:
            # Intento automático: RAG primero, web como fallback
            return await self._handle_auto(query, user_email)

    async def _handle_rag(self, query: str, user_email: str) -> AgentResult:
        """RAG retrieval sobre la colección personal del usuario."""
        if not user_email:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error="user_email requerido para RAG_RETRIEVAL",
            )
        try:
            from agents.researcher.rag_retriever import retrieve_documents
            content = retrieve_documents(user_email, query)
            if not content:
                return AgentResult(
                    success=True,
                    output={"result": "No se encontraron documentos relevantes.", "source": "rag"},
                    agent_name=self.name,
                    metadata={"hits": 0},
                )
            return AgentResult(
                success=True,
                output={"result": content, "source": "rag"},
                agent_name=self.name,
                metadata={"hits": 1},
            )
        except Exception as exc:
            logger.error(f"[researcher] RAG error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )

    async def _handle_web(self, query: str) -> AgentResult:
        """Búsqueda web vía DuckDuckGo con fast-path de tipo de cambio."""
        try:
            from agents.researcher.web_searcher import web_search
            content = web_search(query)
            return AgentResult(
                success=True,
                output={"result": content, "source": "web"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[researcher] WEB_SEARCH error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )

    async def _handle_auto(self, query: str, user_email: str) -> AgentResult:
        """RAG primero; si no hay hits → búsqueda web."""
        if user_email:
            rag_result = await self._handle_rag(query, user_email)
            if rag_result.success and rag_result.output:
                hits = rag_result.metadata.get("hits", 0)
                if hits > 0:
                    return rag_result
        return await self._handle_web(query)
