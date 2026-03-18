"""
ToolRegistry — registro global de tools externas.

Patrón idéntico al SkillRegistry y AgentRegistry:
  - Decorador @ToolRegistry.register para auto-registro al importar la clase
  - get(name) → instancia singleton (lazy)
  - get_or_none(name) → None si no existe
  - health_check_all() → dict con estado de cada tool

Uso:
    # Registro (en tool.py de cada tool)
    @ToolRegistry.register
    class PrometheusTool(BaseTool): ...

    # Consumo
    from tools.registry import ToolRegistry
    prom = ToolRegistry.get("prometheus")
    output = await prom.execute(QueryInput(promql="up"))

    # En startup de la aplicación
    from tools.registry import register_all_tools
    register_all_tools()
"""
from __future__ import annotations

import logging

from core.exceptions import AmaelError
from core.tool_base import BaseTool

logger = logging.getLogger("tools.registry")


class ToolNotFoundError(AmaelError):
    """Se lanza cuando se solicita una tool no registrada."""


class ToolRegistry:
    """
    Registro global de tools externas.

    Características:
      - Registro via decorador @ToolRegistry.register (sin instanciar)
      - Instanciación lazy: la tool se crea la primera vez que se solicita
      - Singletons: misma instancia reutilizada para todas las requests
      - health_check_all(): verifica conectividad de todas las tools registradas
    """

    _classes:   dict[str, type[BaseTool]] = {}   # nombre → clase
    _instances: dict[str, BaseTool]       = {}   # nombre → instancia singleton

    # ── Registro ──────────────────────────────────────────────────────────────

    @classmethod
    def register(cls, tool_class: type[BaseTool]) -> type[BaseTool]:
        """
        Decorador para registrar una Tool.

        Uso:
            @ToolRegistry.register
            class PrometheusTool(BaseTool):
                name = "prometheus"
        """
        name = getattr(tool_class, "name", "")
        if not name:
            raise ValueError(
                f"La tool '{tool_class.__name__}' debe definir el atributo 'name'"
            )
        if name in cls._classes:
            logger.debug(f"[tool_registry] Tool '{name}' ya registrada — sobreescribiendo.")
        cls._classes[name] = tool_class
        logger.debug(f"[tool_registry] Tool registrada: '{name}' ({tool_class.__name__})")
        return tool_class

    # ── Acceso ────────────────────────────────────────────────────────────────

    @classmethod
    def get(cls, name: str) -> BaseTool:
        """
        Retorna la instancia singleton de la tool.
        Lanza ToolNotFoundError si la tool no está registrada.
        """
        if name not in cls._instances:
            if name not in cls._classes:
                available = list(cls._classes.keys())
                raise ToolNotFoundError(
                    f"Tool '{name}' no registrada. Disponibles: {available}"
                )
            cls._instances[name] = cls._classes[name]()
            logger.debug(f"[tool_registry] Tool '{name}' instanciada.")
        return cls._instances[name]

    @classmethod
    def get_or_none(cls, name: str) -> BaseTool | None:
        """Retorna la instancia singleton o None si no está registrada."""
        try:
            return cls.get(name)
        except ToolNotFoundError:
            return None

    # ── Introspección ─────────────────────────────────────────────────────────

    @classmethod
    def names(cls) -> list[str]:
        """Lista de nombres de tools registradas."""
        return list(cls._classes.keys())

    @classmethod
    def all_tools(cls) -> dict[str, BaseTool]:
        """Retorna un dict con todas las tools instanciadas."""
        return {name: cls.get(name) for name in cls._classes}

    @classmethod
    def unregister(cls, name: str) -> None:
        """Elimina una tool del registro (útil en tests)."""
        cls._classes.pop(name, None)
        cls._instances.pop(name, None)

    @classmethod
    def clear(cls) -> None:
        """Limpia el registro completo (útil en tests)."""
        cls._classes.clear()
        cls._instances.clear()

    # ── Health check ──────────────────────────────────────────────────────────

    @classmethod
    async def health_check_all(cls) -> dict[str, bool]:
        """
        Ejecuta health_check() en todas las tools registradas.

        Returns:
            Dict {tool_name: bool} — True si la tool está disponible.
        """
        results: dict[str, bool] = {}
        for name in cls._classes:
            try:
                tool    = cls.get(name)
                healthy = await tool.health_check()
                results[name] = healthy
                if not healthy:
                    logger.warning(f"[tool_registry] Tool '{name}' health_check FAIL")
            except Exception as exc:
                logger.error(f"[tool_registry] Tool '{name}' health_check error: {exc}")
                results[name] = False
        return results


# ── Registro de tools predefinidas ────────────────────────────────────────────

def register_all_tools() -> None:
    """
    Importa (y por tanto registra via @ToolRegistry.register) todas las tools
    predefinidas de la plataforma. Llamar una vez en el startup de la aplicación.

    Orden de registro no importa — cada tool es independiente.
    """
    _tools = [
        ("tools.prometheus.tool",  "PrometheusTool"),
        ("tools.grafana.tool",     "GrafanaTool"),
        ("tools.whatsapp.tool",    "WhatsAppTool"),
        ("tools.github.tool",      "GitHubTool"),
        ("tools.piper.tool",       "PiperTool"),
        ("tools.cosyvoice.tool",   "CosyVoiceTool"),  # TTS alta calidad → notas de voz WA
    ]

    for module_path, class_name in _tools:
        try:
            import importlib
            mod = importlib.import_module(module_path)
            # La importación ya dispara @ToolRegistry.register si está decorado,
            # pero si no usa el decorador, lo registramos manualmente.
            tool_class = getattr(mod, class_name, None)
            if tool_class and tool_class.name not in ToolRegistry._classes:
                ToolRegistry.register(tool_class)
            logger.debug(f"[tool_registry] Módulo '{module_path}' importado.")
        except Exception as exc:
            logger.warning(
                f"[tool_registry] No se pudo cargar '{module_path}.{class_name}': {exc}"
            )
