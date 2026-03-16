"""
skills.base — re-exporta los contratos base desde core.skill_base.
Importar desde aquí mantiene las importaciones dentro del paquete skills/.
"""
from core.skill_base import BaseSkill, SkillInput, SkillOutput

__all__ = ["BaseSkill", "SkillInput", "SkillOutput"]
