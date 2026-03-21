"""
Router /api/slo — endpoints de observabilidad y SLO status.

Endpoints:
  GET /api/slo/status — estado actual de todos los SLOs con datos de Prometheus
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.observability")

router = APIRouter(prefix="/api", tags=["observability"])


@router.get("/slo/status")
async def get_slo_status(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """
    Retorna el estado actual de los SLOs con datos en tiempo real de Prometheus.

    - status: ok | at_risk | breached | no_data
    - error_budget_remaining_pct: % del error budget restante en la ventana de 24h
    - meets_availability / meets_latency: null si no hay datos suficientes
    """
    from observability.slo import get_slo_status
    return {"slos": get_slo_status()}
