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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.planner")

router = APIRouter(prefix="/api/planner", tags=["planner"])

# Usuario de servicio: existe para que el bridge y los CronJobs firmen JWT, no
# es una persona con calendario ni con WhatsApp.
_BOT_USER = "bot-amael@richardx.dev"


class PlannerResult(BaseModel):
    processed: int
    results:   list[dict[str, Any]]


def _check_planner_rate_limit() -> None:
    """
    Protección extra para /daily: máximo 1 ejecución cada 20 horas.
    Previene abuso si INTERNAL_API_SECRET se compromete.
    """
    try:
        from storage.redis.client import get_client
        redis = get_client()
        key = "rate_limit:planner_daily"
        if redis.exists(key):
            ttl = redis.ttl(key)
            raise HTTPException(
                status_code=429,
                detail=f"Day planner ya ejecutado. Próximo disponible en {ttl}s",
                headers={"Retry-After": str(ttl)},
            )
        redis.setex(key, 72000, "1")  # TTL 20 horas
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"[planner] Rate limit check falló (Redis): {exc}")


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
    _check_planner_rate_limit()
    from storage.postgres.client import get_connection

    user_emails: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Solo quien PIDIÓ el brief. Antes bastaba con estar activo, así
                # que la plataforma le mandaba un WhatsApp a las 7am a gente que
                # nunca lo solicitó — y por el bug del teléfono (ver
                # _send_brief_whatsapp) todos esos mensajes caían en el número
                # del admin. El bot de servicio no es una persona: no recibe nada.
                cur.execute(
                    """
                    SELECT user_id FROM user_profile
                    WHERE status = 'active'
                      AND daily_brief_enabled = TRUE
                      AND user_id <> %s
                    ORDER BY user_id
                    """,
                    (_BOT_USER,),
                )
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
    Envía el brief del día al WhatsApp del usuario (best-effort).

    El tipo de identidad es 'whatsapp' — es lo que escribe el bridge. Antes se
    consultaba 'phone', un tipo que NADIE escribe: la consulta no devolvía nada
    nunca y los seis briefs caían al fallback `admin_phone`. O sea que el
    resumen de calendario y correo de cada persona se entregaba en el teléfono
    del admin en vez del suyo, cada día laborable a las 7am.

    Por eso ya no hay fallback: sin número propio no se manda nada. Entregar el
    brief de alguien más al admin es peor que no entregarlo.
    """
    import httpx

    from config.settings import settings
    from storage.postgres.client import get_connection

    try:
        phone: str | None = None
        with get_connection() as conn:
            with conn.cursor() as cur:
                # ORDER BY id: hay usuarios con más de un número registrado y un
                # LIMIT 1 sin orden devuelve una fila arbitraria — el brief se
                # iría un día a un número y otro día a otro. Gana el primero
                # que se dio de alta.
                cur.execute(
                    """
                    SELECT identity_value FROM user_identities
                    WHERE canonical_user_id = %s
                      AND identity_type = 'whatsapp'
                    ORDER BY id
                    LIMIT 1
                    """,
                    (user_email,),
                )
                row = cur.fetchone()
                if row:
                    phone = row[0]

        if not phone:
            logger.warning(
                f"[planner] {user_email} pidió el brief pero no tiene WhatsApp "
                f"registrado — omitido (no se redirige a nadie más)")
            return

        wa_url = getattr(settings, "whatsapp_bridge_url", "http://whatsapp-bridge-service:3000")
        resp = httpx.post(
            f"{wa_url}/send",
            json={"phoneNumber": phone, "text": f"📅 *Brief del día*\n\n{summary}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            logger.info(f"[planner] Brief enviado por WhatsApp a {phone}")
        else:
            logger.warning(f"[planner] WhatsApp /send respondió {resp.status_code} para {phone}")
    except Exception as exc:
        logger.warning(f"[planner] No se pudo enviar brief por WhatsApp: {exc}")
