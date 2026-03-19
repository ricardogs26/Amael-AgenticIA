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
from typing import Any

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

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
- Despliegue: Kubernetes con ingress Nginx, Kong (LLM adapter), cert-manager TLS, Vault para secretos"""


@AgentRegistry.register
class UrielAgent(BaseAgent):
    """
    Uriel — Arch Agent: diseño de sistemas, ADRs, patrones y contratos de API.

    task dict esperado:
        {
            "query":      str,   # pregunta o tarea de diseño arquitectónico
            "user_email": str,   # para búsqueda RAG de documentación existente
        }
    """

    name         = "uriel"
    role         = "Arquitectura de software: diseño de sistemas, ADRs y patrones de diseño"
    version      = "1.0.0"
    capabilities = [
        "system_design",
        "adr_generation",
        "design_patterns",
        "api_contracts",
        "rag_retrieval",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name, error="query vacía")

        rag_context = await retrieve_rag_context(user_email, query, k=5, agent_name=self.name)
        prompt      = build_prompt(
            _SYSTEM_PROMPT, query, rag_context,
            context_header="## Documentación existente",
            question_header="## Consulta de arquitectura",
        )

        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "arch_agent"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[arch] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))
