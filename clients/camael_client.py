"""
clients.camael_client — Abstracción de agents/devops/ (Camael) detrás de feature flag.

Función crítica: `handoff_to_camael()` — reemplaza la llamada in-process desde
`agents/sre/scheduler.py:420` y `agents/sre/healer.py:579` por HTTP cuando
CAMAEL_MODE=remote, con fallback a WAL genérico si camael-service está caído.

Fase 3: CAMAEL_MODE={inprocess|remote} controla el routing. El fallback usa
el WAL genérico `storage.redis.wal` (topics: `handoff`, `rfc_update`).

API pública:
    handoff_to_camael(anomaly, incident_key, notify_fn)   → None
    get_handoff_status(incident_key)                       → dict
    get_pending_handoff_count()                            → int  (para métricas)
    drain_pending_handoffs()                               → int  (procesa WAL handoff)
    drain_pending_rfc_updates()                            → int  (procesa WAL rfc_update)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from config.settings import settings

logger = logging.getLogger("clients.camael")


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
    """Encola el handoff en el WAL para replay posterior (topic 'handoff')."""
    from storage.redis import wal
    return wal.enqueue("handoff", incident_key, payload)


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
    """Número de handoffs pendientes en el WAL (para dashboard / alertas)."""
    from storage.redis import wal
    return wal.pending_count("handoff")


def drain_pending_handoffs() -> int:
    """
    Drena handoffs encolados en el WAL reintentando POST /api/camael/handoff.
    Llamado al arranque de camael-service y cada 5min (APScheduler tick).
    """
    if _is_inprocess():
        return 0

    from storage.redis import wal

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
    except Exception as exc:
        logger.error(f"[camael_client] drain: http client FALLÓ: {exc}")
        return 0

    def _consume(payload: dict[str, Any]) -> bool:
        try:
            resp = client.post("/api/camael/handoff", json=payload)
            if resp.status_code in (200, 202):
                logger.info(
                    f"[camael_client] drain: re-enviado {payload.get('incident_key')}"
                )
                return True
            if resp.status_code == 400:
                # Issue no soportado — evitar loop infinito, aceptar el drain.
                logger.info(
                    f"[camael_client] drain: descartado (400) {payload.get('incident_key')}"
                )
                return True
            return False
        except Exception as exc:
            logger.warning(f"[camael_client] drain consume FALLÓ: {exc}")
            return False

    return wal.drain("handoff", _consume)


def drain_pending_rfc_updates() -> int:
    """
    Drena actualizaciones de RFC encoladas en el WAL (topic 'rfc_update').
    El payload guarda `_sys_id` dentro para que el consumer reconstruya la URL.
    Llamado al arranque de camael-service y cada 5min.
    """
    if _is_inprocess():
        return 0

    from storage.redis import wal

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
    except Exception as exc:
        logger.error(f"[camael_client] drain_rfc: http client FALLÓ: {exc}")
        return 0

    def _consume(payload: dict[str, Any]) -> bool:
        sys_id = payload.get("_sys_id")
        if not sys_id:
            logger.warning("[camael_client] drain_rfc: payload sin _sys_id — descartando")
            return True  # descartar, no reintentar indefinidamente
        try:
            body = {k: v for k, v in payload.items() if k != "_sys_id"}
            resp = client.patch(f"/api/camael/rfc/{sys_id}", json=body)
            if resp.status_code in (200, 204, 404):
                return True
            return False
        except Exception as exc:
            logger.warning(f"[camael_client] drain_rfc consume FALLÓ: {exc}")
            return False

    return wal.drain("rfc_update", _consume)


# ── update_rfc — Raphael cierra / marca review del RFC post-verificación ──────

async def update_rfc(
    sys_id: str,
    result: str,
    message: str,
    deployment: str | None = None,
    namespace: str | None = None,
) -> None:
    """
    Actualiza el estado de un RFC ServiceNow tras la verificación post-deploy.

    Contrato (reemplaza `agents/sre/healer._update_rfc_state` + import directo
    de agents.devops.servicenow_client):

    - result="closed": el deployment está sano 5min post-deploy → cerrar RFC
    - result="review": verificación falló → marcar para revisión manual

    En CAMAEL_MODE=inprocess llama al módulo servicenow_client local.
    En CAMAEL_MODE=remote hace PATCH /api/camael/rfc/{sys_id}; si falla,
    encola en WAL (topic "rfc_update", key=sys_id).

    Idempotencia: ServiceNow acepta transiciones repetidas al mismo estado
    sin error; el WAL dedup por sys_id cubre el resto.
    """
    if _is_inprocess():
        try:
            from agents.devops import servicenow_client as sn
            if not sn.is_configured():
                return
            if result == "closed":
                await sn.close_rfc(sys_id, message)
            elif result == "review":
                await sn.fail_rfc(sys_id, message)
            else:
                logger.warning(f"[camael_client] update_rfc result inválido: {result}")
        except Exception as exc:
            logger.warning(f"[camael_client] update_rfc inprocess FALLÓ: {exc}")
        return

    # ── Remote path ────────────────────────────────────────────────────────────
    payload: dict[str, Any] = {"result": result, "message": message}
    if deployment is not None:
        payload["deployment"] = deployment
    if namespace is not None:
        payload["namespace"] = namespace

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
        resp = client.patch(f"/api/camael/rfc/{sys_id}", json=payload)
        if resp.status_code in (200, 204):
            logger.info(f"[camael_client] update_rfc OK {sys_id} result={result}")
            return
        if resp.status_code == 404:
            logger.warning(f"[camael_client] RFC {sys_id} no existe en Camael — skip")
            return
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.error(f"[camael_client] update_rfc FALLÓ {sys_id}: {exc}")
        from storage.redis import wal
        wal_payload = {**payload, "_sys_id": sys_id}
        wal.enqueue("rfc_update", sys_id, wal_payload)
