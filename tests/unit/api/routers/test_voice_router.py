"""POST /api/voice/read — «léeme esto» (idea 3)."""
from __future__ import annotations

import os

for _k, _v in {
    "INTERNAL_API_SECRET": "x" * 32, "JWT_SECRET_KEY": "x" * 32,
    "SESSION_SECRET_KEY": "x" * 32, "POSTGRES_PASSWORD": "t",
    "MINIO_ACCESS_KEY": "t", "MINIO_SECRET_KEY": "t",
}.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    from interfaces.api import auth as auth_mod
    from interfaces.api.routers import chat
    from interfaces.api.routers.voice import router

    monkeypatch.setattr(auth_mod, "_emit_security_event", lambda *a, **k: None)
    calls = []

    async def fake_send(phone, text):
        calls.append((phone, text))
        return "clone"

    monkeypatch.setattr(chat, "_send_voice_note", fake_send)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.calls = calls
    return c


def _auth():
    from config.settings import settings
    return {"Authorization": f"Bearer {settings.internal_api_secret}"}


def test_sin_secreto_403(client):
    r = client.post("/api/voice/read", json={"phone": "5215550001111", "text": "hola"})
    assert r.status_code in (401, 403)


def test_lee_el_texto_tal_cual(client):
    r = client.post("/api/voice/read", headers=_auth(),
                    json={"phone": "130554506788994@lid", "text": "  Léeme esto, por favor.  "})
    assert r.status_code == 200 and r.json() == {"sent": True, "engine": "clone"}
    assert client.calls == [("130554506788994@lid", "Léeme esto, por favor.")]


def test_texto_vacio_400(client):
    r = client.post("/api/voice/read", headers=_auth(), json={"phone": "5215550001111", "text": "   "})
    assert r.status_code in (400, 422)


def test_palabras_clave_de_lectura():
    from interfaces.api.routers.chat import _is_voice_request
    for q in ("Léeme mis correos", "leeme el resumen del documento", "read me my emails",
              "léelo en voz alta"):
        assert _is_voice_request(q), q
    assert not _is_voice_request("¿Qué tengo en el calendario?")
