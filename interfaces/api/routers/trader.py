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
