"""
Router /api/trader — proxy autenticado hacia trader-service.

El trader-service (:8003) es interno y solo acepta INTERNAL_API_SECRET.
Este router expone su API al frontend con la autenticación JWT de usuario:

  GET  /api/trader/status | /account | /orders | /equity
  POST /api/trader/chat | /halt | /resume | /run-cycle

Los POST de control (halt/resume/run-cycle) y el chat quedan igualmente
detrás del JWT — cualquier usuario autorizado de la whitelist puede usarlos
(mismo criterio que el resto de agentes del frontend).
"""
from __future__ import annotations

import logging
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.trader")

router = APIRouter(prefix="/api/trader", tags=["trader"])

TRADER_SERVICE_URL = os.environ.get("TRADER_SERVICE_URL", "http://trader-service:8003")
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET", "")

_TIMEOUT = httpx.Timeout(90.0, connect=5.0)  # el chat con qwen3:14b puede tardar


async def _forward(method: str, path: str, json_body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {INTERNAL_API_SECRET}"}
    url = f"{TRADER_SERVICE_URL}/api/trader{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        logger.error(f"[trader.proxy] {method} {path} → {exc}")
        raise HTTPException(status_code=502, detail="trader-service no disponible") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return r.json()


@router.get("/status")
async def status(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    return await _forward("GET", "/status")


@router.get("/account")
async def account(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    return await _forward("GET", "/account")


@router.get("/orders")
async def orders(user_id: Annotated[str, Depends(get_current_user)], limit: int = 50,
                 include_hold: bool = False) -> dict:
    return await _forward("GET", f"/orders?limit={min(limit, 200)}"
                                 f"&include_hold={str(include_hold).lower()}")


@router.get("/rules")
async def rules(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    """Parámetros de negocio del trader (tabla trader_rules) con metadatos."""
    return await _forward("GET", "/rules")


@router.get("/rules/history")
async def rules_history(user_id: Annotated[str, Depends(get_current_user)],
                        key: str | None = None, limit: int = 50) -> dict:
    q = f"?limit={min(limit, 500)}" + (f"&key={_rule_key(key)}" if key else "")
    return await _forward("GET", f"/rules/history{q}")


@router.patch("/rules/{key}")
async def rules_set(user_id: Annotated[str, Depends(get_current_user)], key: str,
                    body: dict) -> dict:
    """Cambia un parámetro. `changed_by` es el usuario del JWT, no lo que mande el cliente."""
    payload = {"value": body.get("value"), "reason": body.get("reason", ""), "changed_by": user_id}
    return await _forward("PATCH", f"/rules/{_rule_key(key)}", payload)


def _rule_key(key: str) -> str:
    import re
    if not re.fullmatch(r"[a-z0-9_]{1,64}", key or ""):
        raise HTTPException(status_code=422, detail="clave de regla inválida")
    return key


@router.get("/statement")
async def statement(user_id: Annotated[str, Depends(get_current_user)], month: str) -> dict:
    """Estado de cuenta mensual (JSON). month = YYYY-MM."""
    return await _forward("GET", f"/statement?month={_month(month)}")


@router.get("/statement.pdf")
async def statement_pdf(user_id: Annotated[str, Depends(get_current_user)], month: str) -> Response:
    """Estado de cuenta mensual en PDF — se reenvían los bytes tal cual."""
    m = _month(month)
    headers = {"Authorization": f"Bearer {INTERNAL_API_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{TRADER_SERVICE_URL}/api/trader/statement.pdf?month={m}",
                                 headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="trader-service no disponible") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return Response(content=r.content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="amael-trader-{m}.pdf"'})


def _month(month: str) -> str:
    """Valida YYYY-MM antes de concatenarlo en la URL interna."""
    import re
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(status_code=422, detail="month debe ser YYYY-MM")
    return month


@router.get("/holds")
async def holds(user_id: Annotated[str, Depends(get_current_user)], hours: int = 24) -> dict:
    return await _forward("GET", f"/holds?hours={min(hours, 720)}")


@router.get("/equity")
async def equity(user_id: Annotated[str, Depends(get_current_user)], limit: int = 500) -> dict:
    return await _forward("GET", f"/equity?limit={min(limit, 2000)}")


@router.get("/fees")
async def fees(user_id: Annotated[str, Depends(get_current_user)], days: int = 7) -> dict:
    return await _forward("GET", f"/fees?days={min(days, 90)}")


@router.post("/chat")
async def chat(payload: dict, user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    logger.info(f"[trader.chat] user={user_id}")
    return await _forward("POST", "/chat", json_body=payload)


@router.post("/halt")
async def halt(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    logger.warning(f"[trader.halt] solicitado por user={user_id}")
    return await _forward("POST", "/halt")


@router.post("/resume")
async def resume(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    logger.warning(f"[trader.resume] solicitado por user={user_id}")
    return await _forward("POST", "/resume")


@router.post("/run-cycle")
async def run_cycle(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    logger.info(f"[trader.run-cycle] solicitado por user={user_id}")
    return await _forward("POST", "/run-cycle")
