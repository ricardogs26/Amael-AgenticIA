"""
Cliente ServiceNow REST API para gestión de Change Requests (RFC) ITIL v4.

Tablas usadas:
  change_request — RFC (Change Management)
  sys_user       — usuarios (para buscar assignment groups)

Variables de entorno requeridas:
  SERVICENOW_BASE_URL  — https://<instancia>.service-now.com
  SERVICENOW_USER      — usuario de servicio (admin en dev)
  SERVICENOW_PASSWORD  — contraseña
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("devops.servicenow")

_SN_BASE = ""
_SN_USER = ""
_SN_PASS = ""


def _cfg() -> tuple[str, str, str]:
    """Lee configuración de entorno. Lazy para no fallar en import."""
    base = os.environ.get("SERVICENOW_BASE_URL", "").rstrip("/")
    user = os.environ.get("SERVICENOW_USER", "")
    pwd  = os.environ.get("SERVICENOW_PASSWORD", "")
    return base, user, pwd


def is_configured() -> bool:
    base, user, pwd = _cfg()
    return bool(base and user and pwd)


# ── Estados RFC ITIL v4 ───────────────────────────────────────────────────────

class RFCState:
    DRAFT      = "-5"
    NEW        = "1"
    ASSESS     = "2"
    AUTHORIZE  = "3"
    SCHEDULED  = "4"
    IMPLEMENT  = "5"
    REVIEW     = "6"
    CLOSED     = "7"
    CANCELLED  = "8"

    LABELS = {
        "-5": "Draft",
        "1":  "New",
        "2":  "Assess",
        "3":  "Authorize",
        "4":  "Scheduled",
        "5":  "Implement",
        "6":  "Review",
        "7":  "Closed",
        "8":  "Cancelled",
    }

    @classmethod
    def label(cls, state: str) -> str:
        return cls.LABELS.get(str(state), f"State({state})")


# ── CRUD change_request ───────────────────────────────────────────────────────

async def create_rfc(payload: dict) -> dict:
    """
    Crea un RFC en ServiceNow.

    Retorna: {"sys_id": "...", "number": "CHG0030002", "url": "https://..."}
    """
    base, user, pwd = _cfg()
    if not base:
        logger.warning("[sn] SERVICENOW_BASE_URL no configurado — RFC omitido")
        return {"sys_id": "", "number": "N/A", "url": ""}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{base}/api/now/table/change_request",
            auth=(user, pwd),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            logger.error(f"[sn] create_rfc HTTP {r.status_code}: {r.text[:300]}")
            return {"sys_id": "", "number": "ERROR", "url": ""}

        result = r.json().get("result", {})
        sys_id = result.get("sys_id", "")
        number = _field(result, "number")
        url    = f"{base}/nav_to.do?uri=change_request.do?sys_id={sys_id}"
        logger.info(f"[sn] RFC creado: {number} ({sys_id})")
        return {"sys_id": sys_id, "number": number, "url": url}


async def update_rfc(sys_id: str, payload: dict) -> bool:
    """
    Actualiza campos de un RFC existente.
    Para añadir work note: payload={"work_notes": "mensaje"}.
    Para cambiar estado: payload={"state": RFCState.IMPLEMENT}.
    Retorna True si OK.
    """
    if not sys_id:
        return False
    base, user, pwd = _cfg()
    if not base:
        return False

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(
            f"{base}/api/now/table/change_request/{sys_id}",
            auth=(user, pwd),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            logger.error(f"[sn] update_rfc HTTP {r.status_code}: {r.text[:300]}")
            return False
        logger.debug(f"[sn] RFC {sys_id} actualizado: {list(payload.keys())}")
        return True


async def get_rfc(sys_id: str) -> dict:
    """Obtiene campos clave de un RFC."""
    if not sys_id:
        return {}
    base, user, pwd = _cfg()
    if not base:
        return {}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{base}/api/now/table/change_request/{sys_id}",
            auth=(user, pwd),
            headers={"Accept": "application/json"},
            params={"sysparm_fields": "number,state,short_description,sys_id,work_notes,type"},
        )
        if r.status_code >= 400:
            return {}
        result = r.json().get("result", {})
        return {
            "sys_id":            result.get("sys_id", ""),
            "number":            _field(result, "number"),
            "state":             _field(result, "state"),
            "state_label":       RFCState.label(_field(result, "state")),
            "short_description": _field(result, "short_description"),
            "type":              _field(result, "type"),
            "url":               f"{base}/nav_to.do?uri=change_request.do?sys_id={result.get('sys_id','')}",
        }


async def add_work_note(sys_id: str, note: str) -> bool:
    """Añade una work note al RFC (sin cambiar estado)."""
    return await update_rfc(sys_id, {"work_notes": note})


async def advance_rfc_to_assess(sys_id: str) -> None:
    """
    Avanza el RFC desde Draft (-5) → New (1) → Assess (2).
    ServiceNow no acepta saltar estados en un solo PATCH.
    """
    await update_rfc(sys_id, {"state": RFCState.NEW})
    await update_rfc(sys_id, {"state": RFCState.ASSESS})


async def advance_rfc_to_implement(sys_id: str, work_note: str = "") -> None:
    """
    Avanza el RFC: Assess → Authorize → Scheduled → Implement.
    Llamar cuando el operador aprueba el PR.
    """
    await update_rfc(sys_id, {"state": RFCState.AUTHORIZE})
    await update_rfc(sys_id, {"state": RFCState.SCHEDULED})
    payload: dict = {"state": RFCState.IMPLEMENT}
    if work_note:
        payload["work_notes"] = work_note
    await update_rfc(sys_id, payload)


async def advance_rfc_to_closed(sys_id: str, close_notes: str) -> None:
    """
    Cierra el RFC tras verificación exitosa: Implement → Review → Closed.
    """
    await update_rfc(sys_id, {"state": RFCState.REVIEW})
    await update_rfc(sys_id, {
        "state":       RFCState.CLOSED,
        "close_notes": close_notes,
        "work_notes":  close_notes,
    })


async def get_emergency_chg_model() -> str:
    """Retorna el sys_id del Change Model 'Emergency' en ServiceNow."""
    base, user, pwd = _cfg()
    if not base:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{base}/api/now/table/chg_model",
                auth=(user, pwd),
                headers={"Accept": "application/json"},
                params={
                    "sysparm_query": "nameSTARTSWITHEmergency",
                    "sysparm_fields": "sys_id,name",
                    "sysparm_limit": "1",
                },
            )
            results = r.json().get("result", [])
            return results[0].get("sys_id", "") if results else ""
    except Exception:
        return ""


async def close_rfc(sys_id: str, close_notes: str) -> bool:
    """Cierra el RFC como exitoso (ITIL v4: Review → Closed)."""
    return await update_rfc(sys_id, {
        "state":       RFCState.CLOSED,
        "close_notes": close_notes,
        "work_notes":  close_notes,
    })


async def fail_rfc(sys_id: str, reason: str) -> bool:
    """Marca el RFC en revisión con nota de fallo."""
    return await update_rfc(sys_id, {
        "state":      RFCState.REVIEW,
        "work_notes": f"[FALLO] {reason}",
    })


# ── Helper interno ────────────────────────────────────────────────────────────

def _field(result: dict, key: str) -> str:
    """ServiceNow devuelve campos como string o como {'value': ..., 'display_value': ...}."""
    val = result.get(key, "")
    if isinstance(val, dict):
        return val.get("display_value") or val.get("value") or ""
    return str(val) if val else ""
