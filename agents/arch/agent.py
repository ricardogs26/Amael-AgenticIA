"""
ArchAgent — Agente de arquitectura de software: diseño de sistemas, ADRs y patrones.

Responsabilidades:
  - Diseñar arquitecturas de sistemas y componentes
  - Redactar ADRs (Architecture Decision Records)
  - Evaluar patrones de diseño y su aplicabilidad
  - Definir contratos de API y estructuras de datos
  - Usar contexto del proyecto (RAG) para mantener coherencia arquitectónica

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("arch", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.arch.agent")

_SYSTEM_PROMPT = """Eres el arquitecto de software de Amael-IA, una plataforma multi-agente con
LangGraph desplegada en Kubernetes. Tu especialidad es el diseño de sistemas escalables y mantenibles.

Directrices:
- Aplica principios SOLID, Clean Architecture y Domain-Driven Design cuando sea apropiado
- Cuando propongas una arquitectura, documenta los trade-offs explícitamente
- Para ADRs usa el formato: Contexto → Decisión → Consecuencias
- Define contratos de API con tipos explícitos (Pydantic v2 / TypeScript)
- Prefiere soluciones simples que resuelvan el problema actual sobre sobre-ingeniería
- Responde siempre en el mismo idioma que la pregunta

Arquitectura actual del sistema:
- Patrón: LangGraph multi-agente (Planner → Grouper → Batch Executor → Supervisor)
- Comunicación: HTTP REST entre microservicios, JWT para autenticación inter-servicio
- Almacenamiento: PostgreSQL (estado), Redis (caché/dedup), Qdrant (vectores), MinIO (objetos)
- Observabilidad: Prometheus metrics, OTel traces (Tempo), Grafana dashboards
- Despliegue: Kubernetes con ingress Kong, cert-manager TLS, Vault para secretos"""


@AgentRegistry.register
class ArchAgent(BaseAgent):
    """
    Agente de arquitectura de software: diseño, ADRs, patrones y contratos de API.

    task dict esperado:
        {
            "query":      str,   # pregunta o tarea de diseño arquitectónico
            "user_email": str,   # para búsqueda RAG de documentación existente
        }
    """

    name         = "arch"
    role         = "Arquitectura de software: diseño de sistemas, ADRs y patrones de diseño"
    version      = "1.0.0"
    capabilities = [
        "system_design",
        "adr_generation",
        "design_patterns",
        "api_contracts",
        "rag_retrieval",
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

        # 1. Recuperar documentación arquitectónica existente via RAG
        rag_context = ""
        if user_email:
            try:
                from agents.researcher.rag_retriever import retrieve_documents
                rag_context = retrieve_documents(user_email, query, k=5) or ""
            except Exception as exc:
                logger.debug(f"[arch] RAG no disponible: {exc}")

        # 2. Construir prompt
        prompt = _build_prompt(_SYSTEM_PROMPT, query, rag_context)

        # 3. Invocar LLM
        try:
            response = await _invoke_llm(prompt, self.context)
            return AgentResult(
                success=True,
                output={"response": response, "source": "arch_agent"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[arch] LLM error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )


def _build_prompt(system: str, question: str, rag_context: str) -> str:
    parts = [system, ""]
    if rag_context:
        parts += ["## Documentación existente", rag_context, ""]
    parts += ["## Consulta de arquitectura", question]
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
        logger.debug(f"[arch] ChatOllama falló, intentando OllamaLLM: {exc}")

    if context.llm is not None:
        result = await asyncio.to_thread(context.llm.invoke, prompt)
        return str(result)

    raise RuntimeError("No hay LLM disponible en el contexto")
