"""
SRE Reporter — persiste incidentes, genera postmortems y envía notificaciones.

Migrado desde k8s-agent/main.py:
  store_incident()                  — INSERT en sre_incidents (P1)
  _update_incident_verification()   — UPDATE action_result con resultado (P3-A)
  get_recent_incidents()            — SELECT últimos N incidentes
  _generate_postmortem_sync()       — generación LLM de postmortem (P5-D)
  _generate_and_store_postmortem()  — wrapper async con ThreadPoolExecutor
  get_recent_postmortems()          — SELECT últimos N postmortems
  notify_whatsapp_sre()             — alerta por WhatsApp con filtro de severidad
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("agents.sre.reporter")

_WHATSAPP_BRIDGE_URL = os.environ.get(
    "WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000"
)
_OWNER_PHONE         = os.environ.get("OWNER_PHONE", "")
_MIN_NOTIFY_SEVERITY = os.environ.get("SRE_MIN_NOTIFY_SEVERITY", "HIGH")
_SEVERITY_RANK       = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# Singleton LLM para postmortems
_postmortem_llm = None


def _get_postmortem_llm():
    global _postmortem_llm
    if _postmortem_llm is None:
        from langchain_ollama import OllamaLLM

        from config.settings import settings
        _postmortem_llm = OllamaLLM(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
        )
    return _postmortem_llm


# ── Incidentes ────────────────────────────────────────────────────────────────

def store_incident(
    incident_key: str,
    namespace: str,
    resource_name: str,
    resource_type: str,
    issue_type: str,
    severity: str,
    details: str,
    root_cause: str,
    confidence: float,
    action_taken: str,
    action_result: str,
    notified: bool = False,
) -> None:
    """Persiste un incidente en PostgreSQL (ON CONFLICT DO NOTHING)."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sre_incidents
                        (incident_key, namespace, resource_name, resource_type,
                         issue_type, severity, details, root_cause, confidence,
                         action_taken, action_result, notified)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (incident_key) DO NOTHING;
                    """,
                    (
                        incident_key, namespace, resource_name, resource_type,
                        issue_type, severity, details, root_cause, confidence,
                        action_taken, action_result, notified,
                    ),
                )
    except Exception as exc:
        logger.error(f"[reporter] store_incident error: {exc}")


def update_incident_verification(incident_key: str, verification_result: str) -> None:
    """Actualiza action_result con el resultado de la verificación post-acción."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sre_incidents
                    SET action_result = COALESCE(action_result, '') || ' [verify:' || %s || ']'
                    WHERE incident_key = %s;
                    """,
                    (verification_result, incident_key),
                )
    except Exception as exc:
        logger.error(f"[reporter] update_incident_verification error: {exc}")


def get_recent_incidents(limit: int = 10) -> list[dict]:
    """Retorna los últimos N incidentes de PostgreSQL."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT incident_key, created_at, namespace, resource_name,
                           issue_type, severity, action_taken, action_result, confidence
                    FROM sre_incidents
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        logger.error(f"[reporter] get_recent_incidents error: {exc}")
        return []


def get_historical_success_rate(
    issue_type: str, owner_name: str, namespace: str
) -> float | None:
    """
    Retorna la tasa de éxito [0.0-1.0] de ROLLOUT_RESTART para un recurso/issue.
    Retorna None si no hay datos suficientes.
    """
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT action_result
                    FROM sre_incidents
                    WHERE issue_type    = %s
                      AND namespace     = %s
                      AND resource_name LIKE %s
                      AND action_taken  = 'ROLLOUT_RESTART'
                    ORDER BY created_at DESC
                    LIMIT 10;
                    """,
                    (issue_type, namespace, f"{owner_name}%"),
                )
                rows = cur.fetchall()
        if not rows:
            return None
        total     = len(rows)
        successes = sum(
            1 for (r,) in rows
            if r and "✅" in r and "verify:unresolved" not in r
        )
        return round(successes / total, 3)
    except Exception as exc:
        logger.warning(f"[reporter] get_historical_success_rate error: {exc}")
        return None


# ── Postmortems ───────────────────────────────────────────────────────────────

def _get_incident_by_key(incident_key: str) -> dict | None:
    """Recupera un incidente por su clave única."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM sre_incidents WHERE incident_key = %s LIMIT 1;",
                    (incident_key,),
                )
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
    except Exception as exc:
        logger.error(f"[reporter] _get_incident_by_key error: {exc}")
    return None


def _generate_postmortem_sync(incident: dict) -> dict | None:
    """Llama al LLM para generar un postmortem estructurado (timeout 60s)."""
    import concurrent.futures

    prompt = (
        "Genera un postmortem técnico para este incidente en formato JSON:\n\n"
        f"Incidente: {json.dumps(incident, default=str)[:1500]}\n\n"
        "Responde con JSON exacto con estas claves:\n"
        '{"impact": "...", "timeline": "...", "root_cause_summary": "...", '
        '"resolution": "...", "prevention": "...", "action_items": "..."}\n'
        "Sé específico y técnico. En español."
    )
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_get_postmortem_llm().invoke, prompt)
            raw = future.result(timeout=60)

        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except concurrent.futures.TimeoutError:
        logger.warning(f"[reporter] Postmortem LLM timeout para {incident.get('incident_key')}")
    except Exception as exc:
        logger.error(f"[reporter] _generate_postmortem_sync error: {exc}")
    return None


def _store_postmortem(incident_key: str, incident: dict, postmortem: dict) -> None:
    """Persiste el postmortem en la tabla sre_postmortems."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sre_postmortems
                        (incident_key, namespace, resource_name, issue_type,
                         impact, timeline, root_cause_summary, resolution,
                         prevention, action_items, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (incident_key) DO NOTHING;
                    """,
                    (
                        incident_key,
                        incident.get("namespace"),
                        incident.get("resource_name"),
                        incident.get("issue_type"),
                        postmortem.get("impact", ""),
                        postmortem.get("timeline", ""),
                        postmortem.get("root_cause_summary", ""),
                        postmortem.get("resolution", ""),
                        postmortem.get("prevention", ""),
                        postmortem.get("action_items", ""),
                        json.dumps(postmortem),
                    ),
                )
        from observability.metrics import SRE_POSTMORTEM_TOTAL
        SRE_POSTMORTEM_TOTAL.inc()
        logger.info(f"[reporter] Postmortem guardado para {incident_key}")
    except Exception as exc:
        logger.error(f"[reporter] _store_postmortem error: {exc}")


def generate_and_store_postmortem(incident_key: str) -> None:
    """
    Genera y persiste el postmortem de un incidente.
    Diseñado para ejecutarse en un thread background.
    """
    incident = _get_incident_by_key(incident_key)
    if not incident:
        logger.warning(f"[reporter] Incidente {incident_key} no encontrado.")
        return
    postmortem = _generate_postmortem_sync(incident)
    if postmortem:
        _store_postmortem(incident_key, incident, postmortem)


def get_recent_postmortems(limit: int = 5) -> list[dict]:
    """Retorna los últimos N postmortems."""
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT incident_key, created_at, namespace, resource_name,
                           issue_type, impact, root_cause_summary, resolution,
                           prevention, action_items
                    FROM sre_postmortems
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        logger.error(f"[reporter] get_recent_postmortems error: {exc}")
        return []


# ── Notificaciones WhatsApp ────────────────────────────────────────────────────

def notify_whatsapp_sre(message: str, severity: str = "HIGH") -> bool:
    """
    Envía alerta SRE por WhatsApp con filtro de severidad mínima.
    Retorna True si se envió exitosamente.
    """
    from observability.metrics import SRE_NOTIFY_TOTAL

    sev_rank     = _SEVERITY_RANK.get(severity.upper(), 0)
    min_rank     = _SEVERITY_RANK.get(_MIN_NOTIFY_SEVERITY.upper(), 2)
    if sev_rank < min_rank:
        logger.debug(f"[reporter] Notificación {severity} filtrada (mínimo: {_MIN_NOTIFY_SEVERITY})")
        return False

    if not _OWNER_PHONE or not _WHATSAPP_BRIDGE_URL:
        logger.warning("[reporter] WhatsApp no configurado (OWNER_PHONE / WHATSAPP_BRIDGE_URL).")
        return False

    try:
        import requests as _req
        resp = _req.post(
            f"{_WHATSAPP_BRIDGE_URL}/send",
            json={"phone": _OWNER_PHONE, "message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            SRE_NOTIFY_TOTAL.labels(severity=severity).inc()
            logger.info(f"[reporter] Notificación WhatsApp enviada ({severity})")
            return True
        else:
            logger.warning(f"[reporter] WhatsApp error {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        logger.error(f"[reporter] notify_whatsapp_sre error: {exc}")
    return False
