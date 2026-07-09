"""
Router /api/sre — endpoints del agente SRE autónomo.

Endpoints (todos requieren INTERNAL_API_SECRET salvo /status):
  GET  /api/sre/loop/status      — estado del loop (público interno)
  GET  /api/sre/incidents        — últimos incidentes
  GET  /api/sre/postmortems      — últimos postmortems LLM
  GET  /api/sre/learning/stats   — tasa de éxito por (issue_type, action)
  GET  /api/sre/slo/status       — SLO targets con burn rates actuales
  GET  /api/sre/maintenance      — estado de ventana de mantenimiento
  POST /api/sre/maintenance      — activar ventana (minutos)
  DELETE /api/sre/maintenance    — desactivar ventana
  POST /api/sre/command          — dispatcher de comandos WhatsApp /sre
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from interfaces.api.auth import require_internal_secret, require_operator

logger = logging.getLogger("interfaces.api.sre")

router = APIRouter(prefix="/api/sre", tags=["sre"])


# ── Modelos ───────────────────────────────────────────────────────────────────

class MaintenanceRequest(BaseModel):
    minutes: int = 60

class SRECommandRequest(BaseModel):
    command: str
    phone:   str | None = None
    quoted_text: str | None = None  # texto de la alerta citada (para `silent`)


class AgentModeRequest(BaseModel):
    mode: str


# ── Loop status (no requiere JWT — usado por dashboards internos) ─────────────

@router.get("/loop/status")
def get_loop_status() -> dict[str, Any]:
    """Estado del loop SRE: circuit breaker, mantenimiento, config."""
    try:
        import dataclasses

        from clients.raphael_client import get_loop_state
        state = get_loop_state()
        return dataclasses.asdict(state) if dataclasses.is_dataclass(state) else state
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Incidentes ────────────────────────────────────────────────────────────────

@router.get("/incidents")
def get_incidents(
    limit: int = 5,
    _: Annotated[str, Depends(require_operator)] = "",
) -> list[dict[str, Any]]:
    """Últimos N incidentes desde PostgreSQL."""
    try:
        from clients.raphael_client import get_recent_incidents
        return get_recent_incidents(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Postmortems ───────────────────────────────────────────────────────────────

@router.get("/postmortems")
def get_postmortems(
    limit: int = 3,
    _: Annotated[str, Depends(require_operator)] = "",
) -> list[dict[str, Any]]:
    """Últimos N postmortems generados por LLM."""
    try:
        from clients.raphael_client import get_recent_postmortems
        return get_recent_postmortems(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Learning stats ────────────────────────────────────────────────────────────

@router.get("/learning/stats")
def get_learning_stats(
    days: int = 7,
    _: Annotated[str, Depends(require_operator)] = "",
) -> list[dict[str, Any]]:
    """Tasa de éxito por (issue_type, action) en los últimos N días."""
    try:
        from clients.raphael_client import get_historical_success_rate
        return get_historical_success_rate(days=days)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── SLO ───────────────────────────────────────────────────────────────────────

@router.get("/slo/status")
def get_slo_status(
    _: Annotated[str, Depends(require_operator)] = "",
) -> list[dict[str, Any]]:
    """SLO targets con burn rates actuales desde Prometheus."""
    try:
        from clients.raphael_client import get_slo_burn_rates
        return get_slo_burn_rates()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Maintenance ───────────────────────────────────────────────────────────────

@router.get("/maintenance")
def get_maintenance(
    _: Annotated[str, Depends(require_operator)] = "",
) -> dict[str, Any]:
    """Estado de la ventana de mantenimiento activa."""
    try:
        from clients.raphael_client import get_loop_state
        state = get_loop_state()
        return {
            "active":  state.get("maintenance_active", False),
            "expires": state.get("maintenance_expires"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/maintenance", dependencies=[Depends(require_internal_secret)])
def activate_maintenance_window(body: MaintenanceRequest) -> dict[str, Any]:
    """Activa una ventana de mantenimiento por N minutos."""
    try:
        from clients.raphael_client import activate_maintenance
        activate_maintenance(minutes=body.minutes)
        return {"active": True, "minutes": body.minutes}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/maintenance", dependencies=[Depends(require_internal_secret)])
def deactivate_maintenance_window() -> dict[str, Any]:
    """Desactiva la ventana de mantenimiento."""
    try:
        from clients.raphael_client import deactivate_maintenance
        deactivate_maintenance()
        return {"active": False}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── WhatsApp /sre command dispatcher ─────────────────────────────────────────

def _check_sre_command_rate_limit(phone: str | None) -> None:
    """Rate limit para /sre command: 30 req/min por número de teléfono."""
    try:
        from storage.redis.client import get_client
        redis = get_client()
        key = f"rate_limit:sre_command:{phone or 'unknown'}"
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, 60)
        if count > 30:
            raise HTTPException(
                status_code=429,
                detail="Demasiados comandos SRE. Espera 1 minuto.",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception:
        pass


@router.post("/command", dependencies=[Depends(require_internal_secret)])
async def handle_sre_command(body: SRECommandRequest) -> dict[str, Any]:
    """
    Dispatcher de comandos /sre desde el whatsapp-bridge (paridad con el
    legacy k8s-agent 5.2.0). La lógica vive en agents.sre.commands; en modo
    remote el backend proxy-ea a raphael-service.

    Retorna la respuesta en `reply` (campo que lee el bridge); `response`
    se mantiene por compatibilidad.
    """
    _check_sre_command_rate_limit(body.phone)
    from observability.metrics import SRE_WA_COMMANDS_TOTAL
    cmd = body.command.strip().lower()

    cmd_base = cmd.split()[0] if cmd else "ayuda"
    try:
        SRE_WA_COMMANDS_TOTAL.labels(command=cmd_base).inc()
    except Exception:
        pass

    try:
        from clients.raphael_client import dispatch_sre_command
        reply = dispatch_sre_command(cmd, phone=body.phone, quoted_text=body.quoted_text)
    except Exception as exc:
        logger.error(f"[sre.command] error: {exc}", exc_info=True)
        reply = f"❌ Error procesando comando: {exc}"

    return {"reply": reply, "response": reply}


# ── Modo de autonomía (P8-B) ──────────────────────────────────────────────────

@router.get("/mode")
def get_mode() -> dict[str, Any]:
    """Modo de autonomía actual del agente SRE."""
    try:
        from clients.raphael_client import get_agent_mode_info
        return get_agent_mode_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/mode", dependencies=[Depends(require_internal_secret)])
def set_mode(body: AgentModeRequest) -> dict[str, Any]:
    """Cambia el modo de autonomía: observe | conservative | standard | full."""
    try:
        from clients.raphael_client import set_agent_mode_remote
        return set_agent_mode_remote(body.mode)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
