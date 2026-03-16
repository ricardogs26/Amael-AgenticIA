"""
SkillRegistry — registro global de skills de la plataforma.

Análogo a AgentRegistry pero para skills. Permite:
  - Registrar skills con @SkillRegistry.register
  - Instanciar por nombre: SkillRegistry.get("kubernetes")
  - Health-check de todas las skills: SkillRegistry.health_check_all()
  - Listar skills disponibles: SkillRegistry.list_skills()
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from core.skill_base import BaseSkill
from core.exceptions import AmaelError

logger = logging.getLogger("skills.registry")


class SkillNotFoundError(AmaelError):
    def __init__(self, name: str):
        super().__init__(f"Skill '{name}' no registrada.")
        self.skill_name = name


class SkillRegistry:
    """
    Registro singleton de skills de la plataforma.

    Uso:
        @SkillRegistry.register
        class KubernetesSkill(BaseSkill):
            name = "kubernetes"
            ...

        skill = SkillRegistry.get("kubernetes")
        result = await skill.list_pods(ListPodsInput(namespace="amael-ia"))
    """

    _skills: Dict[str, Type[BaseSkill]] = {}

    @classmethod
    def register(cls, skill_class: Type[BaseSkill]) -> Type[BaseSkill]:
        """Decorator que registra una clase de skill en el registry global."""
        name = getattr(skill_class, "name", "")
        if not name:
            raise ValueError(
                f"La clase {skill_class.__name__} debe definir el atributo 'name' "
                "antes de ser registrada."
            )
        if name in cls._skills:
            logger.warning(
                f"[skill_registry] Skill '{name}' ya registrada. "
                f"Sobreescribiendo con {skill_class.__name__}."
            )
        cls._skills[name] = skill_class
        logger.info(
            f"[skill_registry] Skill registrada: '{name}' "
            f"(clase={skill_class.__name__}, v{skill_class.version})"
        )
        return skill_class

    @classmethod
    def get(cls, name: str) -> BaseSkill:
        """
        Instancia una skill por nombre.

        Returns:
            Instancia de la skill lista para usar.

        Raises:
            SkillNotFoundError si el nombre no está registrado.
        """
        if name not in cls._skills:
            raise SkillNotFoundError(name)
        return cls._skills[name]()

    @classmethod
    def get_or_none(cls, name: str) -> Optional[BaseSkill]:
        """Igual que get() pero retorna None si no existe."""
        if name not in cls._skills:
            return None
        return cls._skills[name]()

    @classmethod
    def list_skills(cls) -> List[Dict]:
        """Retorna metadata de todas las skills registradas."""
        return [
            {
                "name":        sc.name,
                "description": sc.description,
                "version":     sc.version,
            }
            for sc in cls._skills.values()
        ]

    @classmethod
    def names(cls) -> List[str]:
        return sorted(cls._skills.keys())

    @classmethod
    def count(cls) -> int:
        return len(cls._skills)

    @classmethod
    def is_registered(cls, name: str) -> bool:
        return name in cls._skills

    @classmethod
    async def health_check_all(cls) -> Dict[str, bool]:
        """
        Ejecuta health_check() en todas las skills registradas.

        Returns:
            Dict {skill_name: bool} con el resultado de cada check.
        """
        results: Dict[str, bool] = {}
        for name, skill_class in cls._skills.items():
            try:
                skill          = skill_class()
                results[name]  = await skill.health_check()
            except Exception as exc:
                logger.warning(f"[skill_registry] health_check '{name}' error: {exc}")
                results[name] = False
        return results

    @classmethod
    def unregister(cls, name: str) -> None:
        """Elimina una skill del registry (útil en tests)."""
        cls._skills.pop(name, None)

    @classmethod
    def clear(cls) -> None:
        """Limpia todos los registros (útil en tests)."""
        cls._skills.clear()


def register_all_skills() -> None:
    """
    Importa e instala todas las skills built-in de la plataforma.

    Llamar una vez durante el startup de la aplicación.
    Las skills se auto-registran al importarse gracias al decorator.
    """
    from skills.kubernetes.skill import KubernetesSkill
    from skills.rag.skill         import RAGSkill
    from skills.llm.skill         import LLMSkill
    from skills.vault.skill       import VaultSkill
    from skills.web.skill         import WebSkill

    for skill_class in [KubernetesSkill, RAGSkill, LLMSkill, VaultSkill, WebSkill]:
        if not SkillRegistry.is_registered(skill_class.name):
            SkillRegistry.register(skill_class)

    logger.info(
        f"[skill_registry] {SkillRegistry.count()} skills registradas: "
        f"{SkillRegistry.names()}"
    )
