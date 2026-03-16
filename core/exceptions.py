"""
Jerarquía de excepciones de la plataforma Amael-AgenticIA.
"""


class AmaelError(Exception):
    """Excepción base de la plataforma."""
    pass


# ── Agentes ───────────────────────────────────────────────────────────────────

class AgentError(AmaelError):
    """Error genérico de un agente."""
    def __init__(self, agent_name: str, message: str):
        self.agent_name = agent_name
        super().__init__(f"[{agent_name}] {message}")


class AgentNotFoundError(AgentError):
    """El agente solicitado no está registrado."""
    pass


class AgentDependencyError(AgentError):
    """El agente no tiene las skills/tools requeridas."""
    pass


class AgentTimeoutError(AgentError):
    """El agente superó el tiempo máximo de ejecución."""
    pass


# ── Skills ────────────────────────────────────────────────────────────────────

class SkillError(AmaelError):
    """Error genérico de una skill."""
    def __init__(self, skill_name: str, message: str):
        self.skill_name = skill_name
        super().__init__(f"[skill:{skill_name}] {message}")


class SkillNotFoundError(SkillError):
    """La skill solicitada no está registrada."""
    pass


class SkillExecutionError(SkillError):
    """Error durante la ejecución de una skill."""
    pass


# ── Tools ─────────────────────────────────────────────────────────────────────

class ToolError(AmaelError):
    """Error genérico de una tool."""
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"[tool:{tool_name}] {message}")


class ToolNotFoundError(ToolError):
    """La tool solicitada no está registrada."""
    pass


class ToolExecutionError(ToolError):
    """Error durante la ejecución de una tool."""
    pass


# ── Orquestación ──────────────────────────────────────────────────────────────

class OrchestrationError(AmaelError):
    """Error en la capa de orquestación."""
    pass


class WorkflowError(OrchestrationError):
    """Error en la ejecución de un workflow LangGraph."""
    pass


class RoutingError(OrchestrationError):
    """No se pudo determinar el agente destino."""
    pass


# ── Seguridad ─────────────────────────────────────────────────────────────────

class SecurityError(AmaelError):
    """Violación de política de seguridad."""
    pass


class PromptInjectionError(SecurityError):
    """Se detectó un patrón de prompt injection."""
    pass


class RateLimitError(SecurityError):
    """El usuario superó el límite de requests."""
    def __init__(self, user_id: str, limit: int, window: int):
        self.user_id = user_id
        super().__init__(
            f"Rate limit superado para '{user_id}': "
            f"máximo {limit} requests cada {window}s"
        )


# ── Storage ───────────────────────────────────────────────────────────────────

class StorageError(AmaelError):
    """Error de capa de persistencia."""
    pass


class ConnectionError(StorageError):
    """No se pudo establecer conexión con el almacenamiento."""
    pass


# ── LLM ───────────────────────────────────────────────────────────────────────

class LLMError(AmaelError):
    """Error en la capa LLM."""
    pass


class LLMTimeoutError(LLMError):
    """El LLM no respondió en el tiempo esperado."""
    pass


class LLMParseError(LLMError):
    """No se pudo parsear la respuesta del LLM."""
    pass
