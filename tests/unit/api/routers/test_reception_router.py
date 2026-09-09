"""Router /api/reception: rutas registradas y auth por secreto interno."""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret-" + "x" * 20)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")
    from interfaces.api.routers.reception import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _auth():
    from config.settings import settings
    return {"Authorization": f"Bearer {settings.internal_api_secret}"}


def test_rutas_en_openapi(client):
    paths = client.app.openapi()["paths"]
    assert "/api/reception/message" in paths and "/api/reception/command" in paths


def test_sin_secreto_403(client):
    assert client.post("/api/reception/message", json={"phone": "5215550001111", "text": "hola"}).status_code == 403
    assert client.post("/api/reception/command", json={"phone": "5215550001111", "command": ""}).status_code == 403


def test_message_delega_y_nunca_revienta(client, monkeypatch):
    import agents.reception.receptionist as rc
    monkeypatch.setattr(rc, "handle_message", lambda p, t, m: "hola visitante")
    from interfaces.api import auth as auth_mod
    monkeypatch.setattr(auth_mod, "_emit_security_event", lambda *a, **k: None)
    r = client.post("/api/reception/message", json={"phone": "5215550001111", "text": "hola"}, headers=_auth())
    assert r.status_code in (200, 429)
    if r.status_code == 200:
        assert r.json() == {"reply": "hola visitante"}
