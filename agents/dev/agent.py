"""
Gabriel — Agente de desarrollo de software: código, bugs, PRs y refactoring.

Responsabilidades:
  - Escribir, explicar y depurar código
  - Analizar bugs y proponer soluciones
  - Revisar código y sugerir refactoring
  - Implementar funcionalidades con contexto del proyecto (RAG)
  - Buscar referencias técnicas (web search)
  - Leer archivos del repo, crear branches y abrir Pull Requests (GitHubTool v2)

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("gabriel", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.gabriel")

_SYSTEM_PROMPT = """Eres Gabriel, un ingeniero de software senior especializado en Python, FastAPI, Next.js y
arquitecturas de sistemas distribuidos. Trabajas en Amael-IA, una plataforma multi-agente con LangGraph.

Directrices:
- Escribe código limpio, explícito y sin over-engineering
- Prefiere editar archivos existentes antes de crear nuevos
- Incluye solo los comentarios donde la lógica no sea obvia
- Cuando detectes un bug, explica la causa raíz antes de proponer el fix
- Para refactoring, justifica el cambio con el problema concreto que resuelve
- Responde siempre en el mismo idioma que la pregunta
- Para tareas de código en GitHub: usa get_file_contents para leer, create_branch + create_commit + create_pull_request para implementar

Stack técnico del proyecto:
- Backend: Python 3.11, FastAPI, LangGraph, LangChain, Pydantic v2
- Frontend: Next.js 14 (App Router), TypeScript, React 18
- Infra: Kubernetes (MicroK8s), Docker, PostgreSQL, Redis, Qdrant
- LLM: Ollama (qwen2.5:14b), nomic-embed-text para embeddings"""


@AgentRegistry.register
class GabrielAgent(BaseAgent):
    """
    Gabriel — Agente de desarrollo: código, bugs, implementación y PRs en GitHub.

    task dict esperado:
        {
            "query":      str,   # descripción de la tarea de desarrollo
            "user_email": str,   # para búsqueda RAG de contexto del proyecto
        }
    """

    name         = "gabriel"
    role         = "Desarrollo de software: código, bugs, PRs y refactoring en GitHub"
    version      = "2.0.0"
    capabilities = [
        "code_generation",
        "bug_analysis",
        "code_review",
        "refactoring",
        "rag_retrieval",
        "web_search",
        "github_read",
        "github_write",
        "create_pull_request",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name, error="query vacía")

        rag_context = await retrieve_rag_context(user_email, query, k=4, agent_name=self.name)
        prompt      = build_prompt(
            _SYSTEM_PROMPT, query, rag_context,
            context_header="## Contexto del proyecto",
            question_header="## Tarea",
        )

        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "gabriel"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[gabriel] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))
