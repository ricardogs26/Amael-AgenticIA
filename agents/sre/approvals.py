"""
SRE Approvals — cola de aprobaciones humanas vía WhatsApp (P7-S3, portado
del legacy k8s-agent).

Las acciones que requieren aprobación (SCALE_UP, PATCH_RESOURCES) se guardan
en Redis con TTL de 15 minutos. El usuario responde por WhatsApp:
    /sre si       — ejecutar la acción pendiente
    /sre no       — cancelar
    /sre rollout  — ejecutar solo ROLLOUT_RESTART (opción conservadora)
    /sre pendiente — listar aprobaciones pendientes
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

logger = logging.getLogger("agents.sre.approvals")

APPROVAL_TTL = 900  # 15 minutos para que el usuario responda
_KEY_PREFIX = "sre:approval:"


def _redis():
    from storage.redis.client import get_client
    return get_client()


def save_pending_approval(
    incident_key: str,
    action: str,
    resource: str,
    namespace: str,
    issue_type: str,
    root_cause: str = "",
    confidence: float = 0.0,
) -> None:
    """Guarda una acción pendiente de aprobación en Redis (TTL 15min)."""
    data = {
        "incident_key": incident_key,
        "action":       action,
        "resource":     resource,
        "namespace":    namespace,
        "issue_type":   issue_type,
        "root_cause":   root_cause,
        "confidence":   confidence,
        "created_at":   datetime.now(UTC).isoformat(),
    }
    try:
        _redis().setex(f"{_KEY_PREFIX}{incident_key}", APPROVAL_TTL, json.dumps(data))
    except Exception as exc:
        logger.error(f"[approvals] Error guardando aprobación: {exc}")


def get_pending_approval(approval_id: str | None = None) -> dict | None:
    """Devuelve la aprobación pendiente más reciente (o la indicada por approval_id)."""
    try:
        redis = _redis()
        if approval_id:
            raw = redis.get(f"{_KEY_PREFIX}{approval_id}")
            return json.loads(raw) if raw else None
        best, best_ts = None, ""
        for key in redis.keys(f"{_KEY_PREFIX}*"):
            raw = redis.get(key)
            if not raw:
                continue
            data = json.loads(raw)
            ts = data.get("created_at", "")
            if ts > best_ts:
                best, best_ts = data, ts
        return best
    except Exception as exc:
        logger.debug(f"[approvals] Error leyendo aprobación: {exc}")
        return None


def list_pending_approvals() -> list[dict]:
    """Lista todas las aprobaciones pendientes, más recientes primero."""
    try:
        redis = _redis()
        results = []
        for key in redis.keys(f"{_KEY_PREFIX}*"):
            raw = redis.get(key)
            if raw:
                results.append(json.loads(raw))
        return sorted(results, key=lambda x: x.get("created_at", ""), reverse=True)
    except Exception:
        return []


def remove_approval(incident_key: str) -> None:
    """Elimina una aprobación pendiente."""
    try:
        _redis().delete(f"{_KEY_PREFIX}{incident_key}")
    except Exception as exc:
        logger.debug(f"[approvals] Error eliminando aprobación: {exc}")
