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
from typing import Annotated, Dict, Any, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.planner")

router = APIRouter(prefix="/api/planner", tags=["planner"])


class PlannerResult(BaseModel):
    processed: int
    results:   List[Dict[str, Any]]


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
    from config.settings import settings

    results: List[Dict[str, Any]] = []
    processed = 0

    for user_email in settings.full_whitelist:
        if "@" not in user_email:
            continue   # skips phone numbers

        try:
            from agents.productivity.day_planner import run_day_planner
            summary = await run_day_planner(user_email=user_email)
            results.append({"user": user_email, "status": "ok", "summary": summary})
            processed += 1
            logger.info(f"[planner] Plan generado para {user_email}")
        except Exception as exc:
            logger.error(f"[planner] Error en plan para {user_email}: {exc}")
            results.append({"user": user_email, "status": "error", "error": str(exc)})

    return PlannerResult(processed=processed, results=results)
