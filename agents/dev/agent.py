"""
DevAgent — Agente de desarrollo de software: código, bugs, PRs y refactoring.

Responsabilidades:
  - Escribir, explicar y depurar código
  - Analizar bugs y proponer soluciones
  - Revisar código y sugerir refactoring
  - Implementar funcionalidades con contexto del proyecto (RAG)
  - Buscar referencias técnicas (web search)

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("dev", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.dev.agent")

_SYSTEM_PROMPT = """Eres un ingeniero de software senior especializado en Python, FastAPI, Next.js y
arquitecturas de sistemas distribuidos. Trabajas en Amael-IA, una plataforma multi-agente con LangGraph.

Directrices:
- Escribe código limpio, explícito y sin over-engineering
- Prefiere editar archivos existentes antes de crear nuevos
- Incluye solo los comentarios donde la lógica no sea obvia
- Cuando detectes un bug, explica la causa raíz antes de proponer el fix
- Para refactoring, justifica el cambio con el problema concreto que resuelve
- Responde siempre en el mismo idioma que la pregunta

Stack técnico del proyecto:
- Backend: Python 3.11, FastAPI, LangGraph, LangChain, Pydantic v2
- Frontend: Next.js 14 (App Router), TypeScript, React 18
- Infra: Kubernetes (MicroK8s), Docker, PostgreSQL, Redis, Qdrant
- LLM: Ollama (qwen2.5:14b), nomic-embed-text para embeddings"""


@AgentRegistry.register
class DevAgent(BaseAgent):
    """
    Agente de desarrollo: código, bugs, implementación y refactoring.

    task dict esperado:
        {
            "query":      str,   # descripción de la tarea de desarrollo
            "user_email": str,   # para búsqueda RAG de contexto del proyecto
        }
    """

    name         = "dev"
    role         = "Desarrollo de software: código, bugs, PRs y refactoring"
    version      = "1.0.0"
    capabilities = [
        "code_generation",
        "bug_analysis",
        "code_review",
        "refactoring",
        "rag_retrieval",
        "web_search",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error="query vacía",
            )

        # 1. Recuperar contexto del proyecto via RAG
        rag_context = ""
        if user_email:
            try:
                from agents.researcher.rag_retriever import retrieve_documents
                rag_context = retrieve_documents(user_email, query, k=4) or ""
            except Exception as exc:
                logger.debug(f"[dev] RAG no disponible: {exc}")

        # 2. Construir prompt
        prompt = _build_prompt(_SYSTEM_PROMPT, query, rag_context)

        # 3. Invocar LLM
        try:
            response = await _invoke_llm(prompt, self.context)
            return AgentResult(
                success=True,
                output={"response": response, "source": "dev_agent"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[dev] LLM error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )


def _build_prompt(system: str, question: str, rag_context: str) -> str:
    parts = [system, ""]
    if rag_context:
        parts += ["## Contexto del proyecto", rag_context, ""]
    parts += ["## Tarea", question]
    return "\n".join(parts)


async def _invoke_llm(prompt: str, context: AgentContext) -> str:
    """Invoca el LLM disponible en el contexto o via ChatOllama directamente."""
    import asyncio
    import os

    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOllama(
            model=os.environ.get("MODEL_NAME", "qwen2.5:14b"),
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434"),
        )
        lines = prompt.split("\n")
        system_lines, rest_lines = [], []
        in_system = True
        for line in lines:
            if in_system and line.startswith("##"):
                in_system = False
            (system_lines if in_system else rest_lines).append(line)

        system_text   = "\n".join(system_lines).strip()
        question_text = "\n".join(rest_lines).strip()

        messages = [SystemMessage(content=system_text), HumanMessage(content=question_text)]
        result   = await asyncio.to_thread(llm.invoke, messages)
        return result.content if hasattr(result, "content") else str(result)

    except Exception as exc:
        logger.debug(f"[dev] ChatOllama falló, intentando OllamaLLM: {exc}")

    if context.llm is not None:
        result = await asyncio.to_thread(context.llm.invoke, prompt)
        return str(result)

    raise RuntimeError("No hay LLM disponible en el contexto")
