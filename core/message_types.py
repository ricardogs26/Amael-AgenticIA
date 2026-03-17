"""
Tipos de mensajes para comunicación entre agentes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.constants import MessageType


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Mensaje base ──────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    """Mensaje base para comunicación entre agentes."""
    from_agent: str
    to_agent: str
    message_type: MessageType
    payload: dict[str, Any]
    id: str = field(default_factory=_uuid)
    correlation_id: str = field(default_factory=_uuid)
    timestamp: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Request / Response ────────────────────────────────────────────────────────

@dataclass
class TaskRequest(AgentMessage):
    """
    Solicitud de tarea enviada a un agente.

    Campos adicionales:
      task_type  — tipo de tarea ("plan", "execute", "diagnose", etc.)
      priority   — 1 (baja) a 10 (crítica)
      timeout_s  — segundos máximos de espera
      context    — datos extra que el agente puede necesitar
    """
    task_type: str = ""
    priority: int = 5
    timeout_s: int = 120
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.message_type = MessageType.REQUEST


@dataclass
class TaskResult(AgentMessage):
    """
    Resultado de una tarea ejecutada por un agente.

    Campos adicionales:
      success     — True si la tarea se completó sin errores
      result      — payload de la respuesta (tipo específico por agente)
      error       — mensaje de error si success=False
      duration_ms — tiempo de ejecución en milisegundos
    """
    success: bool = True
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0

    def __post_init__(self):
        self.message_type = MessageType.RESPONSE if self.success else MessageType.ERROR


# ── Eventos ───────────────────────────────────────────────────────────────────

@dataclass
class AgentEvent(AgentMessage):
    """
    Evento asíncrono emitido por un agente (sin esperar respuesta).
    Ej: SREAgent emite AnomalyDetected → WhatsApp Tool lo consume.
    """
    event_name: str = ""

    def __post_init__(self):
        self.message_type = MessageType.EVENT


# ── Modelos de request específicos ───────────────────────────────────────────

@dataclass
class ChatRequest:
    """Request del usuario al OrchestratorAgent vía API."""
    question: str
    user_id: str
    conversation_id: str
    request_id: str = field(default_factory=_uuid)
    attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Respuesta final al usuario."""
    answer: str
    request_id: str
    success: bool = True
    error: str | None = None
    supervisor_score: int | None = None
    agents_used: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
