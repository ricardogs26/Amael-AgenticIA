"""
BaseAgent — contrato base para todos los agentes de la plataforma.

Cada agente hereda de BaseAgent e implementa execute().
El ciclo de vida completo (before → execute → after / on_error)
se dispara llamando a run().
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.exceptions import AgentDependencyError

logger = logging.getLogger(__name__)


# ── Contexto de ejecución ─────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """
    Contexto inyectado en cada agente al momento de instanciarlo.

    Contiene todas las dependencias que el agente puede necesitar:
    LLM, skills, tools, memoria y metadatos del request.
    """
    user_id: str
    conversation_id: str
    request_id: str
    llm: Any                            # BaseLLMAdapter
    skills: dict[str, Any] = field(default_factory=dict)   # name → BaseSkill
    tools: dict[str, Any] = field(default_factory=dict)    # name → BaseTool
    memory: Any | None = None        # MemoryStore (Qdrant + Redis + Postgres)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Resultado estandarizado ───────────────────────────────────────────────────

@dataclass
class AgentResult:
    """
    Resultado estandarizado retornado por cualquier agente.

    Permite a la capa de orquestación procesar respuestas de forma
    uniforme sin conocer los detalles internos de cada agente.
    """
    success: bool
    output: Any
    agent_name: str
    duration_ms: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sub_results: list[AgentResult] = field(default_factory=list)

    def __repr__(self) -> str:
        status = "OK" if self.success else f"ERROR: {self.error}"
        return f"<AgentResult agent={self.agent_name} {status} {self.duration_ms:.0f}ms>"


# ── Contrato base ─────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Contrato base para todos los agentes de Amael-AgenticIA.

    Implementación mínima requerida:
        class MyAgent(BaseAgent):
            name = "my_agent"
            role = "Descripción del rol"

            async def execute(self, task: Dict[str, Any]) -> AgentResult:
                ...

    Metadata de clase (class-level):
        name            — identificador único en el registry
        role            — descripción del rol del agente
        version         — semver del agente
        capabilities    — lista de capacidades declaradas
        required_skills — skills que deben estar en context.skills
        required_tools  — tools que deben estar en context.tools
    """

    # Metadatos — deben ser sobreescritos en cada subclase
    name: str = ""
    role: str = ""
    version: str = "1.0.0"
    capabilities: list[str] = []
    required_skills: list[str] = []
    required_tools: list[str] = []

    def __init__(self, context: AgentContext) -> None:
        if not self.name:
            raise ValueError(
                f"{self.__class__.__name__} debe definir el atributo 'name'"
            )
        self.context = context
        self._validate_dependencies()
        self._logger = logging.getLogger(f"agent.{self.name}")

    def _validate_dependencies(self) -> None:
        """Verifica que el contexto tiene las skills y tools requeridas."""
        missing_skills = [
            s for s in self.required_skills
            if s not in self.context.skills
        ]
        if missing_skills:
            raise AgentDependencyError(
                self.name,
                f"Skills requeridas no encontradas: {missing_skills}. "
                f"Disponibles: {list(self.context.skills.keys())}",
            )

        missing_tools = [
            t for t in self.required_tools
            if t not in self.context.tools
        ]
        if missing_tools:
            raise AgentDependencyError(
                self.name,
                f"Tools requeridas no encontradas: {missing_tools}. "
                f"Disponibles: {list(self.context.tools.keys())}",
            )

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    @abstractmethod
    async def execute(self, task: dict[str, Any]) -> AgentResult:
        """
        Ejecuta la tarea principal del agente.

        Args:
            task: Parámetros de la tarea. Estructura definida por cada agente
                  (generalmente un modelo Pydantic validado previamente).

        Returns:
            AgentResult con el resultado estandarizado.
        """
        ...

    async def before_execute(self, task: dict[str, Any]) -> None:
        """
        Hook pre-ejecución.
        Ideal para: validación adicional, inicio de span OTel, logging.
        """
        self._logger.debug(
            "Iniciando ejecución",
            extra={"agent": self.name, "user_id": self.context.user_id},
        )

    async def after_execute(
        self, task: dict[str, Any], result: AgentResult
    ) -> None:
        """
        Hook post-ejecución.
        Ideal para: métricas, guardado en memoria, cleanup de recursos.
        """
        self._logger.debug(
            "Ejecución completada",
            extra={
                "agent": self.name,
                "success": result.success,
                "duration_ms": result.duration_ms,
            },
        )

    async def on_error(
        self, task: dict[str, Any], error: Exception
    ) -> AgentResult:
        """
        Hook de manejo de errores.
        Por defecto registra el error y retorna AgentResult(success=False).
        Los agentes pueden sobreescribir para recuperación personalizada.
        """
        self._logger.error(
            f"Error en ejecución: {error}",
            exc_info=True,
            extra={"agent": self.name},
        )
        return AgentResult(
            success=False,
            output=None,
            agent_name=self.name,
            error=str(error),
        )

    async def run(self, task: dict[str, Any]) -> AgentResult:
        """
        Template method: ciclo de vida completo del agente.

        Orden de ejecución:
          1. before_execute (hook)
          2. execute        (implementación del agente)
          3. after_execute  (hook)
          → en caso de excepción: on_error (hook)

        El duration_ms se calcula automáticamente aquí.
        """
        start = datetime.now(UTC)
        try:
            await self.before_execute(task)
            result = await self.execute(task)
        except Exception as exc:
            result = await self.on_error(task, exc)

        elapsed = (datetime.now(UTC) - start).total_seconds() * 1000
        result.duration_ms = elapsed

        try:
            await self.after_execute(task, result)
        except Exception as exc:
            logger.warning(f"[{self.name}] after_execute falló: {exc}")

        return result

    # ── Utilidades ────────────────────────────────────────────────────────────

    def skill(self, name: str) -> Any:
        """Acceso conveniente a una skill del contexto."""
        if name not in self.context.skills:
            raise AgentDependencyError(self.name, f"Skill '{name}' no disponible")
        return self.context.skills[name]

    def tool(self, name: str) -> Any:
        """Acceso conveniente a una tool del contexto."""
        if name not in self.context.tools:
            raise AgentDependencyError(self.name, f"Tool '{name}' no disponible")
        return self.context.tools[name]

    def __repr__(self) -> str:
        return f"<Agent:{self.name} v{self.version}>"
