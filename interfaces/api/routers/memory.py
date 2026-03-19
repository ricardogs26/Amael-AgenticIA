"""
Router /api/memory — gestión de memoria episódica del usuario (Zaphkiel).

Endpoints:
  GET    /api/memory              — lista recuerdos paginados del usuario
  DELETE /api/memory              — elimina TODOS los recuerdos (GDPR wipe)
  DELETE /api/memory/{memory_id}  — elimina un recuerdo específico
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.memory")

router = APIRouter(prefix="/api/memory", tags=["memory"])


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_memories(
    user_id: Annotated[str, Depends(get_current_user)],
    limit:   int = Query(default=20, ge=1, le=100),
    offset:  int = Query(default=0,  ge=0),
):
    """
    Lista los recuerdos almacenados del usuario, ordenados por relevancia de inserción.

    Retorna:
        {memories: [{id, episode_type, content, timestamp, importance, ...}], total: int}
    """
    agent  = _get_zaphkiel(user_id)
    result = await agent.execute({
        "action": "list",
        "user_id": user_id,
        "limit":   limit,
        "offset":  offset if offset > 0 else None,
    })
    if not result.success:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=result.error)
    return result.output


@router.delete("/{memory_id}", status_code=status.HTTP_200_OK)
async def forget_memory(
    memory_id: str,
    user_id:   Annotated[str, Depends(get_current_user)],
):
    """
    Elimina un recuerdo específico del usuario por su ID (UUID).
    """
    agent  = _get_zaphkiel(user_id)
    result = await agent.execute({
        "action":  "forget",
        "user_id": user_id,
        "id":      memory_id,
    })
    if not result.success:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=result.error)
    return result.output


@router.delete("", status_code=status.HTTP_200_OK)
async def forget_all_memories(
    user_id: Annotated[str, Depends(get_current_user)],
):
    """
    Elimina TODOS los recuerdos del usuario (GDPR wipe).
    Borra la colección Qdrant completa de este usuario.
    """
    agent  = _get_zaphkiel(user_id)
    result = await agent.execute({
        "action":  "forget",
        "user_id": user_id,
        # id=None → wipe completo
    })
    if not result.success:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=result.error)
    logger.info(f"[memory] GDPR wipe completado para user={user_id}")
    return result.output


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_zaphkiel(user_id: str):
    """Instancia ZaphkielAgent con un contexto mínimo."""
    try:
        from agents.base.agent_registry import AgentRegistry
        from core.agent_base import AgentContext
        ctx = AgentContext(
            request_id=f"memory-api-{user_id[:8]}",
            user_id=user_id,
        )
        return AgentRegistry.get("zaphkiel", ctx)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ZaphkielAgent no disponible: {exc}",
        )
