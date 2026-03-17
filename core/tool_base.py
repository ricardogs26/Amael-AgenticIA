"""
BaseTool — contrato base para integraciones con sistemas externos.

La diferencia entre Tool y Skill:
  - Skill: capacidad interna de la plataforma (K8s, RAG, LLM, Git)
  - Tool:  integración con sistema externo (Prometheus, Grafana, GitHub, Jira)

Las Tools son stateless, tipadas con Pydantic, y tienen health check propio.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

# ── Modelos I/O base ──────────────────────────────────────────────────────────

class ToolInput(BaseModel):
    """Base para todos los inputs de tools."""
    model_config = {"arbitrary_types_allowed": True}


class ToolOutput(BaseModel):
    """Base para todos los outputs de tools."""
    success: bool
    data: Any = None
    error: str | None = None
    source: str = ""           # Nombre de la tool que generó el resultado
    metadata: dict[str, Any] = {}

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def ok(cls, data: Any, source: str = "", **metadata) -> ToolOutput:
        return cls(success=True, data=data, source=source, metadata=metadata)

    @classmethod
    def fail(cls, error: str, source: str = "", **metadata) -> ToolOutput:
        return cls(success=False, data=None, error=error, source=source, metadata=metadata)


# ── Contrato base ─────────────────────────────────────────────────────────────

class BaseTool(ABC):
    """
    Integración con un sistema externo.

    Implementación mínima:
        class PrometheusTool(BaseTool):
            name = "prometheus"
            description = "PromQL queries contra Prometheus"

            async def execute(self, input: ToolInput) -> ToolOutput:
                ...
    """

    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    external_system: str = ""   # "prometheus", "grafana", "github", etc.

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(
                f"{self.__class__.__name__} debe definir el atributo 'name'"
            )
        self._logger = logging.getLogger(f"tool.{self.name}")

    @abstractmethod
    async def execute(self, input: ToolInput) -> ToolOutput:
        """Ejecuta la integración con el sistema externo."""
        ...

    async def health_check(self) -> bool:
        """
        Verifica conectividad con el sistema externo.
        Debe sobreescribirse en todas las tools que dependen de servicios externos.
        """
        return True

    def __repr__(self) -> str:
        return f"<Tool:{self.name} → {self.external_system} v{self.version}>"
