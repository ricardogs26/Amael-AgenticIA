"""
clients.camael_client — Abstracción de agents/devops/ (Camael) detrás de feature flag.

Función crítica: `handoff_to_camael()` — reemplaza la llamada in-process desde
`agents/sre/scheduler.py:420` y `agents/sre/healer.py:579` por HTTP cuando
AGENTS_MODE=remote, con fallback a queue en Redis si camael-service está caído.

Fase 1: skeleton — AGENTS_MODE=inprocess por default.
Fase 3: se conmuta a remote con canary; el fallback Redis garantiza idempotencia.

API pública:
    handoff_to_camael(anomaly, incident_key, notify_fn)   → None
    get_handoff_status(incident_key)                       → dict
    get_pending_handoff_count()                            → int  (para métricas)
    drain_pending_handoffs()                               → int  (procesa queue)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from config.settings import settings

logger = logging.getLogger("clients.camael")

# Redis key template para queue de handoffs pendientes
_REDIS_PENDING_KEY = "camael:pending_handoff:{incident_key}"
_REDIS_PENDING_TTL = 3600  # 1h — ventana de catch-up


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_inprocess() -> bool:
    """Camael usa su propio flag CAMAEL_MODE independiente de AGENTS_MODE."""
    return settings.camael_mode == "inprocess"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _anomaly_to_handoff_payload(anomaly: Any, incident_key: str) -> dict[str, Any]:
    """
    Mapea un dataclass `Anomaly` al schema `HandoffRequest` del OpenAPI Camael.

    Se defiende con getattr para que cambios en el dataclass de Raphael no
    rompan el contrato. Campos desconocidos van al `context` libre.
    """
    deployment_name = (
        getattr(anomaly, "owner_name", None)
        or getattr(anomaly, "resource_name", None)
        or ""
    )
    return {
        "incident_key":    incident_key,
        "issue_type":      str(getattr(anomaly, "issue_type", "")),
        "severity":        str(getattr(anomaly, "severity", "HIGH")),
        "namespace":       getattr(anomaly, "namespace", "amael-ia"),
        "deployment_name": deployment_name,
        "resource_name":   getattr(anomaly, "resource_name", None),
        "owner_name":      getattr(anomaly, "owner_name", None),
        "reason":          (
            getattr(anomaly, "diagnosis", None)
            or f"{getattr(anomaly, 'issue_type', 'UNKNOWN')} detected by Raphael"
        ),
        "raphael_action":  "ROLLOUT_RESTART",
        "triggered_at":    getattr(anomaly, "detected_at", None) or _now_iso(),
        "context": {
            "confidence":   getattr(anomaly, "confidence", None),
            "metric_value": getattr(anomaly, "metric_value", None),
            "pod_restart_count": getattr(anomaly, "restart_count", None),
        },
    }


def _enqueue_fallback(incident_key: str, payload: dict[str, Any]) -> bool:
    """Encola el handoff en Redis para replay posterior. True si exitoso."""
    try:
        from storage.redis.client import get_client
        r = get_client()
        key = _REDIS_PENDING_KEY.format(incident_key=incident_key)
        r.set(key, json.dumps(payload), ex=_REDIS_PENDING_TTL)
        logger.warning(
            f"[camael_client] handoff encolado en Redis: {incident_key} (TTL {_REDIS_PENDING_TTL}s)"
        )
        return True
    except Exception as exc:
        logger.error(f"[camael_client] fallback Redis FALLÓ: {exc}", exc_info=True)
        return False


# ── API pública ────────────────────────────────────────────────────────────────

def handoff_to_camael(anomaly: Any, incident_key: str, notify_fn: Callable[[str], None]) -> None:
    """
    Dispara un handoff Raphael→Camael.

    Contrato preservado del original `agents/sre/healer.handoff_to_camael()`:
      - anomaly:      dataclass Anomaly con issue_type, resource_name, etc.
      - incident_key: clave única para dedup (Redis + PR description).
      - notify_fn:    función de notificación WhatsApp (para fallbacks).

    Fase 1 (inprocess): delega directo a `healer.handoff_to_camael()`.
    Fase 3 (remote):
      1. POST /api/camael/handoff a camael-service.
      2. Si falla conexión → encola en Redis + notifica humano.
      3. Si Camael retorna 400 (issue no soportado) → log + salida silenciosa.
    """
    if _is_inprocess():
        from agents.sre.healer import handoff_to_camael as _local
        return _local(anomaly, incident_key, notify_fn)

    # ── Remote path ────────────────────────────────────────────────────────────
    payload = _anomaly_to_handoff_payload(anomaly, incident_key)

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
        resp = client.post("/api/camael/handoff", json=payload)

        if resp.status_code in (200, 202):
            data = resp.json() if resp.content else {}
            logger.info(
                f"[camael_client] handoff OK {incident_key} · "
                f"status={data.get('status')} pr_id={data.get('pr_id')}"
            )
            return
        if resp.status_code == 400:
            logger.info(f"[camael_client] Camael rechazó handoff {incident_key}: {resp.text}")
            return
        # 429 / 5xx → fallback a queue
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    except Exception as exc:
        logger.error(f"[camael_client] handoff FALLÓ {incident_key}: {exc}")
        enqueued = _enqueue_fallback(incident_key, payload)
        msg = (
            f"⚠️ *Handoff a Camael pendiente*\n\n"
            f"Raphael ejecutó la remediación temporal pero no pudo contactar a Camael.\n"
            f"Incident: `{incident_key}`\n"
            f"{'Se reintentará automáticamente.' if enqueued else 'Persistencia falló — requiere acción manual.'}"
        )
        try:
            notify_fn(msg)
        except Exception:
            pass


def get_handoff_status(incident_key: str) -> dict[str, Any] | None:
    """
    Consulta el estado de un handoff procesado.
    Retorna None si no existe.
    """
    if _is_inprocess():
        # En inprocess, el estado vive en PostgreSQL tabla camael_gitops_actions
        try:
            from storage.postgres.client import get_connection
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM camael_gitops_actions WHERE incident_key = %s",
                    (incident_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception as exc:
            logger.error(f"[camael_client] get_handoff_status FALLÓ: {exc}")
            return None

    # Remote path
    try:
        from clients._http import get_camael_client
        client = get_camael_client()
        resp = client.get(f"/api/camael/handoff/{incident_key}/status")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error(f"[camael_client] get_handoff_status remote FALLÓ: {exc}")
        return None


def get_pending_handoff_count() -> int:
    """Número de handoffs pendientes en Redis (para dashboard / alertas)."""
    try:
        from storage.redis.client import get_client
        r = get_client()
        keys = r.keys(_REDIS_PENDING_KEY.format(incident_key="*"))
        return len(keys) if keys else 0
    except Exception:
        return 0


def drain_pending_handoffs() -> int:
    """
    Procesa los handoffs encolados en Redis (catch-up tras reconexión con camael-service).
    Llamado al arranque del backend y periódicamente (ej. cada 5min via APScheduler).

    Retorna el número de handoffs reenviados exitosamente.
    """
    if _is_inprocess():
        # En modo inprocess el queue no debería crecer; limpiamos por si quedaron restos.
        return 0

    try:
        from storage.redis.client import get_client
        r = get_client()
        keys = r.keys(_REDIS_PENDING_KEY.format(incident_key="*"))
        if not keys:
            return 0

        from clients._http import get_camael_client
        client = get_camael_client()
        ok_count = 0

        for key in keys:
            raw = r.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw if isinstance(raw, str) else raw.decode())
                resp = client.post("/api/camael/handoff", json=payload)
                if resp.status_code in (200, 202):
                    r.delete(key)
                    ok_count += 1
                    logger.info(f"[camael_client] drain: re-enviado {payload.get('incident_key')}")
            except Exception as exc:
                logger.warning(f"[camael_client] drain: falló {key}: {exc}")

        return ok_count

    except Exception as exc:
        logger.error(f"[camael_client] drain FALLÓ: {exc}")
        return 0
