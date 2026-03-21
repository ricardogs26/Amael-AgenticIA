"""
Router /api/planner — Day Planner diario.

Endpoints:
  POST /api/planner/daily   — invocado por el CronJob (weekdays 7am MX)
                              requiere INTERNAL_API_SECRET

El endpoint instancia ProductivityAgent para cada usuario activo y genera
el plan del día: eventos de calendario + emails + bloque de tareas en LLM.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.planner")

router = APIRouter(prefix="/api/planner", tags=["planner"])


class PlannerResult(BaseModel):
    processed: int
    results:   list[dict[str, Any]]


@router.post(
    "/daily",
    response_model=PlannerResult,
    dependencies=[Depends(require_internal_secret)],
)
async def run_daily_planner() -> PlannerResult:
    """
    Genera el plan diario para todos los usuarios activos con credenciales Google.

    Invocado por el CronJob k8s/28.-day-planner-cronjob.yaml
    cada día laborable a las 7am (America/Mexico_City).

    Requiere: Authorization: Bearer {INTERNAL_API_SECRET}
    """
    from storage.postgres.client import get_connection

    user_emails: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM user_profile WHERE status = 'active'")
                rows = cur.fetchall()
                user_emails = [r[0] for r in rows if "@" in (r[0] or "")]
    except Exception as exc:
        logger.error(f"[planner] Error recuperando usuarios activos: {exc}")
        return PlannerResult(processed=0, results=[{"status": "error", "error": "DB_ERROR"}])

    results: list[dict[str, Any]] = []
    processed = 0

    for user_email in user_emails:

        try:
            from agents.productivity.day_planner import organize_day_for_user
            res = await organize_day_for_user(user_email=user_email)
            summary = res.get("summary", "")
            results.append({"user": user_email, "status": "ok", "summary": summary})
            processed += 1
            logger.info(f"[planner] Plan generado para {user_email}")
            # Enviar brief por WhatsApp (best-effort)
            if summary:
                _send_brief_whatsapp(user_email, summary)
        except Exception as exc:
            logger.error(f"[planner] Error en plan para {user_email}: {exc}")
            results.append({"user": user_email, "status": "error", "error": str(exc)})

    return PlannerResult(processed=processed, results=results)


def _send_brief_whatsapp(user_email: str, summary: str) -> None:
    """
    Envía el brief del día al número WhatsApp del usuario (best-effort).
    Busca el número de teléfono del usuario en user_identities.
    """
    import httpx
    from config.settings import settings

    try:
        # Buscar número de teléfono en user_identities
        from storage.postgres.client import get_connection
        phone: str | None = None
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT identity_value FROM user_identities
                    WHERE canonical_user_id = %s
                      AND identity_type = 'phone'
                    LIMIT 1
                    """,
                    (user_email,),
                )
                row = cur.fetchone()
                if row:
                    phone = row[0]

        if not phone:
            # Fallback: ADMIN_PHONE desde settings
            phone = getattr(settings, "admin_phone", None)

        if not phone:
            logger.warning(f"[planner] No se encontró teléfono para {user_email}, omitiendo WhatsApp")
            return

        wa_url = getattr(settings, "whatsapp_bridge_url", "http://whatsapp-bridge-service:3000")
        resp = httpx.post(
            f"{wa_url}/send",
            json={"phone": phone, "message": f"📅 *Brief del día*\n\n{summary}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            logger.info(f"[planner] Brief enviado por WhatsApp a {phone}")
        else:
            logger.warning(f"[planner] WhatsApp /send respondió {resp.status_code} para {phone}")
    except Exception as exc:
        logger.warning(f"[planner] No se pudo enviar brief por WhatsApp: {exc}")
