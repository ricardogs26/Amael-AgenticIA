"""
ContextFactory — construye AgentContext con skills inyectadas desde SkillRegistry.

Es el puente entre el sistema de skills (Fase 3) y los agentes (Fase 2).
Cada request recibe un contexto fresco con las skills disponibles en ese momento.

Uso:
    ctx = ContextFactory.build_context(
        user_id="user@example.com",
        conversation_id="conv-123",
        request_id="req-abc",
    )
    agent = AgentRegistry.get("raphael", ctx)
    result = await agent.run({"query": "¿Cuál es el estado del clúster?"})
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from core.agent_base import AgentContext

logger = logging.getLogger("orchestration.context_factory")


class ContextFactory:
    """
    Fábrica de AgentContext.

    Inyecta automáticamente:
      - skills disponibles en SkillRegistry
      - LLM del proceso (OllamaLLM singleton vía LLMSkill)
      - metadatos del request (user_id, conversation_id, request_id)

    Uso normal (todas las skills):
        ctx = ContextFactory.build_context(user_id, conversation_id, request_id)

    Uso restringido (subset de skills):
        ctx = ContextFactory.build_context(..., skill_names=["kubernetes", "llm"])

    Uso mínimo (sin skills — health checks, tests):
        ctx = ContextFactory.build_minimal_context(user_id)
    """

    @classmethod
    def build_context(
        cls,
        user_id: str,
        conversation_id: str = "",
        request_id: str = "",
        skill_names: Optional[List[str]] = None,
        extra_tools: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentContext:
        """
        Construye un AgentContext completo con skills inyectadas.

        Args:
            user_id:         Email / ID del usuario propietario del request.
            conversation_id: ID de la conversación activa (para historial).
            request_id:      UUID del request para tracing. Auto-generado si vacío.
            skill_names:     Lista de nombres de skills a inyectar. None = todas.
            extra_tools:     Tools adicionales a incluir en el contexto.
            metadata:        Metadatos extra para el agente.

        Returns:
            AgentContext listo para instanciar cualquier BaseAgent.
        """
        if not request_id:
            request_id = str(uuid.uuid4())

        skills = cls._load_skills(skill_names)
        llm    = cls._get_llm()

        ctx = AgentContext(
            user_id=user_id,
            conversation_id=conversation_id or "",
            request_id=request_id,
            llm=llm,
            skills=skills,
            tools=extra_tools or {},
            metadata={
                "user_id":         user_id,
                "conversation_id": conversation_id,
                **(metadata or {}),
            },
        )
        logger.debug(
            f"[context_factory] Contexto creado para user={user_id!r} "
            f"skills={list(skills.keys())} request={request_id}"
        )
        return ctx

    @classmethod
    def build_minimal_context(
        cls,
        user_id: str = "system",
        request_id: str = "",
    ) -> AgentContext:
        """
        Construye un AgentContext mínimo sin skills — para health checks y tests.
        """
        return AgentContext(
            user_id=user_id,
            conversation_id="",
            request_id=request_id or str(uuid.uuid4()),
            llm=None,
            skills={},
            tools={},
            metadata={},
        )

    @classmethod
    def build_sre_context(
        cls,
        user_id: str = "system",
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para SREAgent:
        kubernetes + llm + vault skills (sin rag ni web que no usa SRE).
        """
        return cls.build_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            skill_names=["kubernetes", "llm", "vault"],
        )

    @classmethod
    def build_productivity_context(
        cls,
        user_id: str,
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para ProductivityAgent: vault + llm skills.
        """
        return cls.build_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            skill_names=["vault", "llm"],
        )

    @classmethod
    def build_researcher_context(
        cls,
        user_id: str,
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para ResearchAgent: rag + web + llm skills.
        """
        return cls.build_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            skill_names=["rag", "web", "llm"],
        )

    @classmethod
    def build_cto_context(
        cls,
        user_id: str,
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para CTOAgent: rag + web + llm skills.
        Alias de build_researcher_context — mismo skill set.
        """
        return cls.build_researcher_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
        )

    @classmethod
    def build_dev_context(
        cls,
        user_id: str,
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para DevAgent: rag + web + llm skills.
        Alias de build_researcher_context — mismo skill set.
        """
        return cls.build_researcher_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
        )

    @classmethod
    def build_arch_context(
        cls,
        user_id: str,
        request_id: str = "",
        conversation_id: str = "",
    ) -> AgentContext:
        """
        Contexto optimizado para ArchAgent: rag + llm skills.
        """
        return cls.build_context(
            user_id=user_id,
            conversation_id=conversation_id,
            request_id=request_id,
            skill_names=["rag", "llm"],
        )

    # ── Helpers privados ──────────────────────────────────────────────────────

    @classmethod
    def _load_skills(cls, skill_names: Optional[List[str]]) -> Dict[str, Any]:
        """
        Instancia y retorna las skills solicitadas desde SkillRegistry.
        Skills no encontradas se omiten con un warning.
        """
        try:
            from skills.registry import SkillRegistry
        except ImportError:
            logger.warning("[context_factory] skills.registry no disponible. Sin skills.")
            return {}

        if skill_names is None:
            # Todas las registradas
            names = SkillRegistry.names()
        else:
            names = skill_names

        skills: Dict[str, Any] = {}
        for name in names:
            skill = SkillRegistry.get_or_none(name)
            if skill is not None:
                skills[name] = skill
            else:
                logger.debug(f"[context_factory] Skill '{name}' no registrada. Omitida.")
        return skills

    @classmethod
    def _get_llm(cls) -> Any:
        """
        Retorna el LLM singleton del proceso (OllamaLLM).
        Compartido entre todos los contextos — evita reconexiones.
        """
        try:
            from skills.registry import SkillRegistry
            llm_skill = SkillRegistry.get_or_none("llm")
            if llm_skill is not None:
                # Exponer el singleton del OllamaLLM directamente
                from skills.llm.skill import _get_ollama_llm
                return _get_ollama_llm()
        except Exception as exc:
            logger.debug(f"[context_factory] LLM via skill no disponible: {exc}")

        # Fallback: instanciar directamente
        try:
            from langchain_ollama import OllamaLLM
            return OllamaLLM(
                model=os.environ.get("MODEL_NAME", "qwen2.5:14b"),
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434"),
            )
        except Exception as exc:
            logger.warning(f"[context_factory] No se pudo crear LLM: {exc}")
            return None
