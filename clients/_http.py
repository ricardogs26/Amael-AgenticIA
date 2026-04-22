"""
clients._http — Helpers internos para clientes HTTP sync/async.

Patrones aplicados:
  - Singleton por servicio vía lru_cache (una conexión persistente por pod).
  - Header Authorization: Bearer <INTERNAL_API_SECRET> siempre presente.
  - Timeouts razonables (30s por default — los handoffs LLM pueden tomar tiempo).
  - En tests, invalidar cache con `get_raphael_client.cache_clear()` para inyectar mocks.

Uso:
    from clients._http import get_raphael_client
    client = get_raphael_client()
    resp = client.get("/api/sre/loop/status")
    resp.raise_for_status()
    data = resp.json()
"""
from __future__ import annotations

from functools import lru_cache

import httpx

from config.settings import settings

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.internal_api_secret}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "User-Agent":    "amael-backend/agents-client",
    }


@lru_cache(maxsize=1)
def get_raphael_client() -> httpx.Client:
    """Cliente HTTP sync singleton hacia raphael-service."""
    return httpx.Client(
        base_url=settings.raphael_service_url,
        timeout=_DEFAULT_TIMEOUT,
        headers=_build_headers(),
    )


@lru_cache(maxsize=1)
def get_camael_client() -> httpx.Client:
    """Cliente HTTP sync singleton hacia camael-service."""
    return httpx.Client(
        base_url=settings.camael_service_url,
        timeout=_DEFAULT_TIMEOUT,
        headers=_build_headers(),
    )


@lru_cache(maxsize=1)
def get_raphael_async_client() -> httpx.AsyncClient:
    """Cliente HTTP async singleton hacia raphael-service."""
    return httpx.AsyncClient(
        base_url=settings.raphael_service_url,
        timeout=_DEFAULT_TIMEOUT,
        headers=_build_headers(),
    )


@lru_cache(maxsize=1)
def get_camael_async_client() -> httpx.AsyncClient:
    """Cliente HTTP async singleton hacia camael-service."""
    return httpx.AsyncClient(
        base_url=settings.camael_service_url,
        timeout=_DEFAULT_TIMEOUT,
        headers=_build_headers(),
    )


def reset_clients() -> None:
    """
    Cierra y reinstancia todos los clientes.
    Usado en tests o al rotar INTERNAL_API_SECRET en runtime.
    """
    for getter in (
        get_raphael_client,
        get_camael_client,
        get_raphael_async_client,
        get_camael_async_client,
    ):
        try:
            client = getter.__wrapped__()  # obtener instancia sin cachear
            # Cerrar clientes cacheados si existen
        except Exception:
            pass
        getter.cache_clear()
