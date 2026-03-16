"""
CTOAgent — Agente estratégico para decisiones tecnológicas y roadmap.

Responsabilidades:
  - Responder preguntas de estrategia tecnológica, roadmap y visión
  - Analizar opciones de arquitectura desde perspectiva ejecutiva
  - Proporcionar análisis de trade-offs con contexto del proyecto (RAG)
  - Consultar tendencias tecnológicas (web search)

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("cto", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.cto.agent")

_SYSTEM_PROMPT = """Eres el CTO de Amael-IA, una plataforma de inteligencia artificial multi-agente
desplegada en Kubernetes. Tu rol es responder con visión estratégica y criterio técnico ejecutivo.

Directrices:
- Prioriza la claridad y el impacto de negocio sobre los detalles técnicos de bajo nivel
- Fundamenta tus decisiones en trade-offs concretos: costo, velocidad, escalabilidad, mantenibilidad
- Cuando hay contexto del proyecto disponible, úsalo para dar recomendaciones específicas
- Proporciona opciones con sus pros y contras cuando la decisión no es obvia
- Responde siempre en el mismo idioma que la pregunta

Plataforma actual:
- Backend: Python/FastAPI con LangGraph multi-agente
- Infra: MicroK8s (single-node), Ollama (qwen2.5:14b), Qdrant, PostgreSQL, Redis
- Observabilidad: Prometheus + Grafana + Tempo (OTel)"""


@AgentRegistry.register
class CTOAgent(BaseAgent):
    """
    Agente de estrategia tecnológica y decisiones de arquitectura ejecutiva.

    task dict esperado:
        {
            "query":      str,   # pregunta o tema estratégico
            "user_email": str,   # para búsqueda RAG personalizada
        }
    """

    name         = "cto"
    role         = "Estrategia tecnológica, roadmap y decisiones ejecutivas de arquitectura"
    version      = "1.0.0"
    capabilities = [
        "tech_strategy",
        "roadmap_planning",
        "architecture_decisions",
        "trade_off_analysis",
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

        # 1. Recuperar contexto RAG del proyecto
        rag_context = ""
        if user_email:
            try:
                from agents.researcher.rag_retriever import retrieve_documents
                rag_context = retrieve_documents(user_email, query, k=4) or ""
            except Exception as exc:
                logger.debug(f"[cto] RAG no disponible: {exc}")

        # 2. Construir prompt con contexto y sistema
        prompt = _build_prompt(_SYSTEM_PROMPT, query, rag_context)

        # 3. Invocar LLM
        try:
            response = await _invoke_llm(prompt, self.context)
            return AgentResult(
                success=True,
                output={"response": response, "source": "cto_agent"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[cto] LLM error: {exc}")
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
    parts += ["## Pregunta", question]
    return "\n".join(parts)


async def _invoke_llm(prompt: str, context: AgentContext) -> str:
    """Invoca el LLM disponible en el contexto o via ChatOllama directamente."""
    import asyncio
    import os

    # Intentar vía ChatOllama con separación de mensajes
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOllama(
            model=os.environ.get("MODEL_NAME", "qwen2.5:14b"),
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434"),
        )
        # Separar system del resto del prompt
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
        logger.debug(f"[cto] ChatOllama falló, intentando OllamaLLM: {exc}")

    # Fallback: OllamaLLM del contexto
    if context.llm is not None:
        result = await asyncio.to_thread(context.llm.invoke, prompt)
        return str(result)

    raise RuntimeError("No hay LLM disponible en el contexto")
