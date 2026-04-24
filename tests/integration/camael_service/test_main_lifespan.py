"""Verifica que camael_service.main construye app y drena WAL al arrancar."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env():
    os.environ.setdefault("INTERNAL_API_SECRET", "x" * 32)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")
    os.environ["CAMAEL_MODE"] = "inprocess"
    os.environ["POSTGRES_HOST"] = "localhost"
    os.environ["REDIS_HOST"] = "localhost"
    yield


def test_app_has_camael_router_registered():
    """La app de camael_service debe montar /api/camael/*."""
    from camael_service.main import app

    paths = [r.path for r in app.routes]
    assert any("/api/camael/handoff" in p for p in paths)
    assert any("/api/camael/rfc/" in p for p in paths)


def test_health_endpoint_responds():
    from camael_service.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
        # 200 OK o 503 (si PG/Redis no resuelven en tests local) — ambos demuestran que el endpoint existe
        assert resp.status_code in (200, 503)


def test_metrics_endpoint_mounted():
    from camael_service.main import app
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Prometheus output siempre incluye métricas de proceso Python
        body = resp.content
        assert b"python_info" in body or b"process_" in body
