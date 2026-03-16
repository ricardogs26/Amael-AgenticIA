"""
BaseSkill — contrato base para todas las skills de la plataforma.

Las skills son capacidades reutilizables, stateless, que pueden ser
compartidas por múltiples agentes. Reciben y retornan modelos Pydantic.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic import BaseModel


# ── Modelos I/O base ──────────────────────────────────────────────────────────

class SkillInput(BaseModel):
    """Base para todos los inputs de skills."""
    model_config = {"arbitrary_types_allowed": True}


class SkillOutput(BaseModel):
    """Base para todos los outputs de skills."""
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = {}

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def ok(cls, data: Any, **metadata) -> "SkillOutput":
        return cls(success=True, data=data, metadata=metadata)

    @classmethod
    def fail(cls, error: str, **metadata) -> "SkillOutput":
        return cls(success=False, data=None, error=error, metadata=metadata)


# ── Contrato base ─────────────────────────────────────────────────────────────

class BaseSkill(ABC):
    """
    Capacidad reutilizable que puede ser usada por múltiples agentes.

    Propiedades:
      - Stateless entre llamadas
      - Input/output tipados con Pydantic
      - Testeable de forma aislada (sin FastAPI, sin LangGraph)
      - Registrable en el SkillRegistry

    Implementación mínima:
        class MySkill(BaseSkill):
            name = "my_skill"
            description = "Lo que hace esta skill"

            async def execute(self, input: SkillInput) -> SkillOutput:
                ...
    """

    name: str = ""
    description: str = ""
    version: str = "1.0.0"

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(
                f"{self.__class__.__name__} debe definir el atributo 'name'"
            )
        self._logger = logging.getLogger(f"skill.{self.name}")

    @abstractmethod
    async def execute(self, input: SkillInput) -> SkillOutput:
        """
        Ejecuta la skill con los parámetros dados.

        Args:
            input: Modelo Pydantic con los parámetros de entrada.
                   El tipo exacto depende de cada skill.

        Returns:
            SkillOutput con success=True/False y data/error.
        """
        ...

    async def health_check(self) -> bool:
        """
        Verifica que la skill puede operar (conectividad, credenciales, etc.).
        Por defecto retorna True. Las skills que dependen de servicios
        externos deben sobreescribir este método.
        """
        return True

    def __repr__(self) -> str:
        return f"<Skill:{self.name} v{self.version}>"
