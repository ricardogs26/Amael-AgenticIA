"""
Registro global de agentes de la plataforma Amael-AgenticIA.

Permite registrar, instanciar y listar todos los agentes disponibles.
Uso del decorator @AgentRegistry.register sobre cada clase de agente.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from core.agent_base import AgentContext, BaseAgent
from core.exceptions import AgentNotFoundError

logger = logging.getLogger("agents.registry")


class AgentRegistry:
    """
    Registro singleton de agentes de la plataforma.

    Ejemplo de registro:
        @AgentRegistry.register
        class SREAgent(BaseAgent):
            name = "sre"
            ...

    Ejemplo de instanciación:
        agent = AgentRegistry.get("raphael", context)
        result = await agent.run(task)
    """

    _agents: Dict[str, Type[BaseAgent]] = {}

    @classmethod
    def register(cls, agent_class: Type[BaseAgent]) -> Type[BaseAgent]:
        """
        Decorator que registra una clase de agente en el registry global.

        Lanza ValueError si el agente no tiene 'name' definido
        o si ya existe otro agente con el mismo nombre.
        """
        name = getattr(agent_class, "name", "")
        if not name:
            raise ValueError(
                f"La clase {agent_class.__name__} debe definir el atributo 'name' "
                f"antes de ser registrada."
            )
        if name in cls._agents:
            logger.warning(
                f"[registry] Agente '{name}' ya registrado. "
                f"Sobreescribiendo con {agent_class.__name__}."
            )
        cls._agents[name] = agent_class
        logger.info(
            f"[registry] Agente registrado: '{name}' "
            f"(clase={agent_class.__name__}, v{agent_class.version})"
        )
        return agent_class

    @classmethod
    def get(cls, name: str, context: AgentContext) -> BaseAgent:
        """
        Instancia un agente registrado con el contexto dado.

        Args:
            name:    Nombre del agente (debe coincidir con agent_class.name).
            context: AgentContext con LLM, skills, tools y metadatos.

        Returns:
            Instancia del agente lista para ejecutar.

        Raises:
            AgentNotFoundError: Si el nombre no está registrado.
        """
        if name not in cls._agents:
            raise AgentNotFoundError(
                name,
                f"Agente '{name}' no encontrado. "
                f"Disponibles: {sorted(cls._agents.keys())}",
            )
        return cls._agents[name](context)

    @classmethod
    def list_agents(cls) -> List[Dict]:
        """
        Retorna metadata de todos los agentes registrados.

        Útil para el endpoint GET /health/agents.
        """
        return [
            {
                "name": agent_cls.name,
                "role": agent_cls.role,
                "version": agent_cls.version,
                "capabilities": agent_cls.capabilities,
                "required_skills": agent_cls.required_skills,
                "required_tools": agent_cls.required_tools,
            }
            for agent_cls in cls._agents.values()
        ]

    @classmethod
    def names(cls) -> List[str]:
        """Retorna la lista de nombres de agentes registrados."""
        return sorted(cls._agents.keys())

    @classmethod
    def count(cls) -> int:
        """Número total de agentes registrados."""
        return len(cls._agents)

    @classmethod
    def is_registered(cls, name: str) -> bool:
        return name in cls._agents

    @classmethod
    def unregister(cls, name: str) -> None:
        """Elimina un agente del registry (útil en tests)."""
        cls._agents.pop(name, None)

    @classmethod
    def clear(cls) -> None:
        """Limpia todos los registros (útil en tests)."""
        cls._agents.clear()


def register_all_agents() -> None:
    """
    Importa todos los módulos de agentes para que sus decoradores
    @AgentRegistry.register se ejecuten.
    Llamar una vez en el startup de la aplicación.
    """
    _agent_modules = [
        ("agents.planner.agent",      "SarielAgent"),
        ("agents.executor.agent",     "ExecutorAgent"),
        ("agents.supervisor.agent",   "RemielAgent"),
        ("agents.researcher.agent",   "SandalphonAgent"),
        ("agents.productivity.agent", "HanielAgent"),
        ("agents.sre.agent",          "RaphaelAgent"),
        ("agents.cto.agent",          "RazielAgent"),
        ("agents.dev.agent",          "GabrielAgent"),
        ("agents.arch.agent",         "UrielAgent"),
        ("agents.memory_agent.agent", "ZaphkielAgent"),
        ("agents.coder.agent",        "JophielAgent"),
        ("agents.devops.agent",       "CamaelAgent"),
    ]
    for module_path, class_name in _agent_modules:
        try:
            import importlib
            importlib.import_module(module_path)
            logger.debug(f"[registry] Módulo '{module_path}' importado.")
        except Exception as exc:
            logger.warning(f"[registry] No se pudo cargar '{module_path}': {exc}")
