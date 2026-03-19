"""
Router /api/agent/task — endpoint de tareas autónomas para agentes especializados.

Permite disparar tareas directamente a un agente por nombre sin pasar por el chat
conversacional. Útil para CI/CD, CronJobs y llamadas inter-agente.

Endpoints:
  POST /api/agent/task   — envía una tarea a un agente específico por nombre
  GET  /api/agent/list   — lista los agentes registrados

Autenticación: Bearer JWT (misma que /api/chat) o INTERNAL_API_SECRET.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.tasks")

router = APIRouter(prefix="/api/agent", tags=["agent-tasks"])


# ── Modelos ───────────────────────────────────────────────────────────────────

class AgentTaskRequest(BaseModel):
    """
    Solicitud de tarea autónoma para un agente específico.

    Campos:
      agent_name  — nombre del agente (gabriel, raphael, uriel, raziel, etc.)
      task        — dict libre con parámetros de la tarea (depende del agente)
      user_id     — usuario propietario del contexto (para RAG y permisos)
    """
    agent_name: str          = Field(..., description="Nombre del agente destino")
    task:       dict[str, Any] = Field(..., description="Parámetros de la tarea (depende del agente)")
    user_id:    str | None  = Field(default=None, description="Sobreescribe el user_id del JWT")


class AgentTaskResponse(BaseModel):
    success:    bool
    agent_name: str
    output:     dict[str, Any] | None
    error:      str | None
    elapsed_ms: float
    request_id: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_user_id(jwt_user: str, body_user_id: str | None, request: Request) -> str:
    """
    Determina el user_id efectivo.
    Si body.user_id está presente y el caller es bot o interno → usar body.user_id.
    En caso contrario usar el JWT user.
    """
    from config.settings import settings
    internal_secret = getattr(settings, "internal_api_secret", "")

    auth_header = request.headers.get("Authorization", "")
    is_internal = internal_secret and auth_header == f"Bearer {internal_secret}"
    is_bot      = jwt_user == "bot-amael@richardx.dev"

    if body_user_id and (is_internal or is_bot):
        return body_user_id
    return jwt_user


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/task", response_model=AgentTaskResponse)
async def agent_task(
    body:    AgentTaskRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_current_user)],
) -> AgentTaskResponse:
    """
    Ejecuta una tarea directamente sobre un agente registrado.

    El agente debe existir en el AgentRegistry (usar GET /api/agent/list para ver disponibles).
    La tarea se ejecuta en modo directo (sin LangGraph pipeline).
    """
    from agents.base.agent_registry import AgentRegistry
    from orchestration.context_factory import ContextFactory

    request_id = str(uuid.uuid4())
    t0         = time.monotonic()

    effective_user = _resolve_user_id(user_id, body.user_id, request)
    agent_name     = body.agent_name.lower().strip()

    if not AgentRegistry.is_registered(agent_name):
        available = AgentRegistry.names()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agente '{agent_name}' no registrado. Disponibles: {available}",
        )

    try:
        ctx   = ContextFactory.build_context(
            user_id=effective_user,
            request_id=request_id,
        )
        agent  = AgentRegistry.get(agent_name, ctx)
        task   = {**body.task, "user_email": body.task.get("user_email", effective_user)}
        result = await agent.run(task)
    except Exception as exc:
        logger.error(f"[tasks] Agente '{agent_name}' error: {exc}")
        return AgentTaskResponse(
            success=False,
            agent_name=agent_name,
            output=None,
            error=str(exc),
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
            request_id=request_id,
        )

    elapsed = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        f"[tasks] agent={agent_name} user={effective_user} "
        f"success={result.success} elapsed={elapsed}ms"
    )
    return AgentTaskResponse(
        success=result.success,
        agent_name=agent_name,
        output=result.output,
        error=result.error,
        elapsed_ms=elapsed,
        request_id=request_id,
    )


@router.get("/list")
async def list_agents(
    _user_id: Annotated[str, Depends(get_current_user)],
) -> list[dict[str, Any]]:
    """Lista todos los agentes registrados con sus capacidades."""
    from agents.base.agent_registry import AgentRegistry
    return AgentRegistry.list_agents()
