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
from typing import Any

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

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
class RazielAgent(BaseAgent):
    """
    Raziel — CTO Agent: estrategia tecnológica y decisiones de arquitectura ejecutiva.

    task dict esperado:
        {
            "query":      str,   # pregunta o tema estratégico
            "user_email": str,   # para búsqueda RAG personalizada
        }
    """

    name         = "raziel"
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

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name, error="query vacía")

        rag_context = await retrieve_rag_context(user_email, query, k=4, agent_name=self.name)
        prompt      = build_prompt(
            _SYSTEM_PROMPT, query, rag_context,
            context_header="## Contexto del proyecto",
            question_header="## Pregunta",
        )

        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "cto_agent"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[cto] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))
