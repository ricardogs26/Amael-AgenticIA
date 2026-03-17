"""
core/ — contratos base de la plataforma Amael-AgenticIA.

Exporta las interfaces que todo agente, skill y tool debe implementar.
"""
from core.agent_base import AgentContext, AgentResult, BaseAgent
from core.constants import (
    MAX_ANSWER_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_GRAPH_ITERATIONS,
    MAX_PLAN_STEPS,
    MAX_PROMPT_CHARS,
    MAX_RETRIES_SUPERVISOR,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    ActionType,
    AnomalyType,
    MessageType,
    Severity,
    StepType,
    SupervisorDecision,
)
from core.exceptions import (
    AgentDependencyError,
    AgentError,
    AgentNotFoundError,
    AgentTimeoutError,
    AmaelError,
    LLMError,
    LLMParseError,
    LLMTimeoutError,
    OrchestrationError,
    PromptInjectionError,
    RateLimitError,
    RoutingError,
    SecurityError,
    SkillError,
    SkillExecutionError,
    SkillNotFoundError,
    StorageError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    WorkflowError,
)
from core.message_types import (
    AgentEvent,
    AgentMessage,
    ChatRequest,
    ChatResponse,
    TaskRequest,
    TaskResult,
)
from core.skill_base import BaseSkill, SkillInput, SkillOutput
from core.tool_base import BaseTool, ToolInput, ToolOutput

__all__ = [
    # Agent
    "BaseAgent", "AgentContext", "AgentResult",
    # Skill
    "BaseSkill", "SkillInput", "SkillOutput",
    # Tool
    "BaseTool", "ToolInput", "ToolOutput",
    # Messages
    "AgentMessage", "TaskRequest", "TaskResult", "AgentEvent",
    "ChatRequest", "ChatResponse",
    # Constants
    "StepType", "ActionType", "AnomalyType", "Severity",
    "SupervisorDecision", "MessageType",
    "MAX_PLAN_STEPS", "MAX_GRAPH_ITERATIONS", "MAX_RETRIES_SUPERVISOR",
    "MAX_PROMPT_CHARS", "MAX_CONTEXT_CHARS", "MAX_ANSWER_CHARS",
    "RATE_LIMIT_MAX", "RATE_LIMIT_WINDOW",
    # Exceptions
    "AmaelError", "AgentError", "AgentNotFoundError",
    "AgentDependencyError", "AgentTimeoutError",
    "SkillError", "SkillNotFoundError", "SkillExecutionError",
    "ToolError", "ToolNotFoundError", "ToolExecutionError",
    "OrchestrationError", "WorkflowError", "RoutingError",
    "SecurityError", "PromptInjectionError", "RateLimitError",
    "StorageError", "LLMError", "LLMTimeoutError", "LLMParseError",
]
