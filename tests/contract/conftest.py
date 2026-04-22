"""
Fixtures para contract tests.

Los contract tests validan que clients/raphael_client.py y clients/camael_client.py
hacen llamadas HTTP que cumplen los contratos definidos en:
  - docs/api/raphael-service.openapi.yaml
  - docs/api/camael-service.openapi.yaml

Sin necesidad de que raphael-service o camael-service estén corriendo.

Estrategia: httpx.MockTransport intercepta requests, los captura para aserciones,
y devuelve respuestas canned que respetan los schemas del OpenAPI.
"""
from __future__ import annotations

import os

# ── Env vars mínimos para que `config.settings` no falle la validación ───────
# Debe ejecutarse ANTES de cualquier import de clients/* o config/*.
os.environ.setdefault("INTERNAL_API_SECRET",  "test-internal-secret-" + "x" * 32)
os.environ.setdefault("JWT_SECRET_KEY",       "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("SESSION_SECRET_KEY",   "test-session-secret-" + "x" * 32)
os.environ.setdefault("POSTGRES_PASSWORD",    "test")
os.environ.setdefault("MINIO_ACCESS_KEY",     "test-minio-access-key")
os.environ.setdefault("MINIO_SECRET_KEY",     "test-minio-secret-key")
os.environ.setdefault("AGENTS_MODE",          "remote")
os.environ.setdefault("RAPHAEL_SERVICE_URL",  "http://raphael-service.test:8002")
os.environ.setdefault("CAMAEL_SERVICE_URL",   "http://camael-service.test:8003")

import httpx
import pytest

from config.settings import settings
from clients import _http


@pytest.fixture(autouse=True)
def _remote_mode():
    """Cada contract test corre en modo `remote`. Restaura al terminar."""
    original = settings.agents_mode
    # pydantic-settings es inmutable en runtime; asignamos el atributo privado
    object.__setattr__(settings, "agents_mode", "remote")
    _http.get_raphael_client.cache_clear()
    _http.get_camael_client.cache_clear()
    yield
    object.__setattr__(settings, "agents_mode", original)
    _http.get_raphael_client.cache_clear()
    _http.get_camael_client.cache_clear()


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    handler,
) -> list[httpx.Request]:
    """
    Instala un httpx.Client con MockTransport para `service` in ['raphael','camael'].
    Retorna una lista que se llena con cada httpx.Request interceptado.
    """
    captured: list[httpx.Request] = []

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    base_url = settings.raphael_service_url if service == "raphael" else settings.camael_service_url
    mock_client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(wrapped_handler),
        headers={
            "Authorization": f"Bearer {settings.internal_api_secret}",
            "Content-Type":  "application/json",
        },
    )

    attr_name = "get_raphael_client" if service == "raphael" else "get_camael_client"
    monkeypatch.setattr(_http, attr_name, lambda: mock_client)
    return captured


@pytest.fixture
def mock_raphael(monkeypatch):
    """Uso: `captured = mock_raphael(handler)` para cada test."""
    def _factory(handler):
        return _install_mock_client(monkeypatch, "raphael", handler)
    return _factory


@pytest.fixture
def mock_camael(monkeypatch):
    """Uso: `captured = mock_camael(handler)` para cada test."""
    def _factory(handler):
        return _install_mock_client(monkeypatch, "camael", handler)
    return _factory
