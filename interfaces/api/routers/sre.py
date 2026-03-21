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


# ── Loop status (no requiere JWT — usado por dashboards internos) ─────────────

@router.get("/loop/status")
def get_loop_status() -> dict[str, Any]:
    """Estado del loop SRE: circuit breaker, mantenimiento, config."""
    try:
        import dataclasses

        from agents.sre import get_loop_state
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
        from agents.sre import get_recent_incidents
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
        from agents.sre import get_recent_postmortems
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
        from agents.sre import get_historical_success_rate
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
        from agents.sre.scheduler import get_slo_burn_rates
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
        from agents.sre import get_loop_state
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
        from agents.sre import activate_maintenance
        activate_maintenance(minutes=body.minutes)
        return {"active": True, "minutes": body.minutes}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/maintenance", dependencies=[Depends(require_internal_secret)])
def deactivate_maintenance_window() -> dict[str, Any]:
    """Desactiva la ventana de mantenimiento."""
    try:
        from agents.sre import deactivate_maintenance
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
    Dispatcher de comandos /sre desde el whatsapp-bridge.

    Comandos soportados:
        status, incidents, postmortems, slo, maintenance on <min>,
        maintenance off, ayuda
    """
    _check_sre_command_rate_limit(body.phone)
    from observability.metrics import SRE_WA_COMMANDS_TOTAL
    cmd = body.command.strip().lower()

    # Extraer comando base (primera palabra)
    cmd_base = cmd.split()[0] if cmd else "ayuda"
    try:
        SRE_WA_COMMANDS_TOTAL.labels(command=cmd_base).inc()
    except Exception:
        pass

    try:
        from agents.sre import (
            activate_maintenance,
            deactivate_maintenance,
            get_loop_state,
            get_recent_incidents,
            get_recent_postmortems,
        )

        if cmd_base == "status":
            return {"response": _format_status(get_loop_state())}

        elif cmd_base == "incidents":
            incidents = get_recent_incidents(limit=5)
            return {"response": _format_incidents(incidents)}

        elif cmd_base == "postmortems":
            pms = get_recent_postmortems(limit=3)
            return {"response": _format_postmortems(pms)}

        elif cmd_base == "slo":
            from agents.sre import load_slo_targets
            targets = load_slo_targets()
            return {"response": f"SLO targets configurados: {len(targets)}"}

        elif cmd_base == "maintenance":
            parts = cmd.split()
            if len(parts) >= 2 and parts[1] == "on":
                minutes = int(parts[2]) if len(parts) > 2 else 60
                activate_maintenance(minutes=minutes)
                return {"response": f"✅ Mantenimiento activado por {minutes} minutos"}
            elif len(parts) >= 2 and parts[1] == "off":
                deactivate_maintenance()
                return {"response": "✅ Mantenimiento desactivado"}
            else:
                return {"response": "Uso: maintenance on <minutos> | maintenance off"}

        elif cmd_base == "ayuda":
            return {"response": _help_text()}

        else:
            return {"response": f"Comando '{cmd_base}' no reconocido. Usa 'ayuda' para ver los disponibles."}

    except Exception as exc:
        logger.error(f"[sre.command] error: {exc}", exc_info=True)
        return {"response": f"❌ Error procesando comando: {exc}"}


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_status(state: dict[str, Any]) -> str:
    cb = state.get("circuit_breaker_state", "CLOSED")
    maint = "🔧 SÍ" if state.get("maintenance_active") else "No"
    loop  = "✅" if state.get("loop_running") else "❌"
    return (
        f"*Estado SRE Agent*\n"
        f"Loop: {loop}\n"
        f"Circuit Breaker: {cb}\n"
        f"Mantenimiento: {maint}\n"
        f"SLOs configurados: {state.get('slo_count', 0)}"
    )


def _format_incidents(incidents: list[dict]) -> str:
    if not incidents:
        return "No hay incidentes recientes."
    lines = ["*Últimos incidentes SRE:*"]
    for inc in incidents:
        lines.append(
            f"• [{inc.get('severity','?')}] {inc.get('issue_type','?')} — "
            f"{inc.get('pod_name','?')} ({inc.get('created_at','?')[:16]})"
        )
    return "\n".join(lines)


def _format_postmortems(pms: list[dict]) -> str:
    if not pms:
        return "No hay postmortems recientes."
    lines = ["*Últimos postmortems:*"]
    for pm in pms:
        lines.append(f"• {pm.get('title','?')} ({pm.get('created_at','?')[:16]})")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "*Comandos /sre disponibles:*\n"
        "• `/sre status` — Estado del loop\n"
        "• `/sre incidents` — Últimos 5 incidentes\n"
        "• `/sre postmortems` — Últimos 3 postmortems\n"
        "• `/sre slo` — Targets SLO\n"
        "• `/sre maintenance on <min>` — Activar mantenimiento\n"
        "• `/sre maintenance off` — Desactivar mantenimiento\n"
        "• `/sre ayuda` — Este mensaje"
    )
