"""
Router /api/camael — endpoints internos del camael-service.

Expone los puntos de entrada que Raphael y backend usan para delegar en Camael
cuando CAMAEL_MODE=remote:

  - POST /api/camael/handoff          — Raphael dispara handoff GitOps
  - PATCH /api/camael/rfc/{sys_id}    — Raphael actualiza RFC post-verificación

Autenticación: Bearer INTERNAL_API_SECRET (mismo esquema que raphael-service).

Nota: NO confundir con /api/devops/* (webhooks GitHub/Bitbucket existentes).
/api/camael/* son endpoints de agente-a-agente; /api/devops/* son webhooks
externos. Ambos viven en el pod camael-service pero tienen consumidores
distintos.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.camael")

router = APIRouter(prefix="/api/camael", tags=["camael"])


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_internal_secret_401(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """
    Wrapper sobre `require_internal_secret` que devuelve HTTP 401 (en vez de 403)
    cuando falta o es inválido el bearer token.

    Razón: los endpoints de Camael son agente-a-agente y deben seguir la
    semántica estándar — 401 = credenciales ausentes/inválidas, 403 =
    autenticado pero sin permiso. El helper global `require_internal_secret`
    usa 403 por compatibilidad con routers previos; aquí preferimos el código
    correcto.
    """
    try:
        require_internal_secret(authorization=authorization)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=exc.detail,
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        raise


# ── Schemas ────────────────────────────────────────────────────────────────────

class HandoffRequest(BaseModel):
    """Request body de POST /api/camael/handoff — contrato con Raphael."""
    incident_key:    str = Field(..., min_length=1, max_length=256)
    issue_type:      str = Field(..., max_length=64)
    severity:        Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    namespace:       str = Field(..., max_length=128)
    deployment_name: str = Field(..., max_length=253)
    resource_name:   str | None = None
    owner_name:      str | None = None
    reason:          str = Field(..., max_length=2048)
    raphael_action:  str = Field(..., max_length=64)
    triggered_at:    str = Field(..., max_length=64)
    context:         dict[str, Any] = Field(default_factory=dict)


class HandoffResponse(BaseModel):
    accepted: bool
    job_id:   str
    pr_id:    str | None = None
    rfc_number: str | None = None


class RfcUpdateRequest(BaseModel):
    result:     Literal["closed", "review"]
    message:    str = Field(..., max_length=2048)
    deployment: str | None = None
    namespace:  str | None = None


class RfcUpdateResponse(BaseModel):
    sys_id: str
    result: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/handoff", response_model=HandoffResponse, status_code=202)
async def handoff(
    payload: HandoffRequest,
    _: Annotated[None, Depends(_require_internal_secret_401)],
) -> HandoffResponse:
    """
    Recibe un handoff desde Raphael y lo procesa vía agents.devops.agent.
    `agent.handle_handoff()` es idempotente por incident_key.
    """
    logger.info(
        f"[camael.handoff] incident={payload.incident_key} "
        f"issue={payload.issue_type} ns={payload.namespace} "
        f"deploy={payload.deployment_name}"
    )

    try:
        from agents.devops.agent import handle_handoff
    except ImportError as exc:
        logger.error(f"[camael.handoff] agents.devops.agent unavailable: {exc}")
        raise HTTPException(status_code=503, detail="camael_core_unavailable") from exc

    try:
        result = await handle_handoff(payload.model_dump())
    except Exception as exc:
        logger.error(f"[camael.handoff] handle_handoff FALLÓ: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"camael_error: {exc}") from exc

    if result is None:
        # Issue no soportado (ej. handoff para tipo que Camael no remedia).
        raise HTTPException(status_code=400, detail="issue_type_not_supported")

    return HandoffResponse(
        accepted=True,
        job_id=f"camael-handoff-{payload.incident_key}",
        pr_id=result.get("pr_id"),
        rfc_number=result.get("rfc_number"),
    )


@router.patch("/rfc/{sys_id}", response_model=RfcUpdateResponse)
async def update_rfc(
    sys_id: str,
    payload: RfcUpdateRequest,
    _: Annotated[None, Depends(_require_internal_secret_401)],
) -> RfcUpdateResponse:
    """
    Actualiza el estado del RFC en ServiceNow.
    Invocado por Raphael al terminar la verificación post-deploy.
    """
    logger.info(
        f"[camael.rfc] sys_id={sys_id} result={payload.result} "
        f"deployment={payload.deployment} ns={payload.namespace}"
    )

    try:
        from agents.devops import servicenow_client as sn
    except ImportError as exc:
        logger.error(f"[camael.rfc] servicenow_client unavailable: {exc}")
        raise HTTPException(status_code=503, detail="servicenow_unavailable") from exc

    if not sn.is_configured():
        raise HTTPException(status_code=503, detail="servicenow_not_configured")

    try:
        if payload.result == "closed":
            await sn.close_rfc(sys_id, payload.message)
        elif payload.result == "review":
            await sn.fail_rfc(sys_id, payload.message)
    except Exception as exc:
        logger.error(f"[camael.rfc] servicenow FALLÓ {sys_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"servicenow_error: {exc}") from exc

    return RfcUpdateResponse(sys_id=sys_id, result=payload.result)
