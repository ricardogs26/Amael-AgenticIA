"""POST /api/english/answer."""
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

REC = {
    "id": "EN-2026-10-08", "phrase": "circle back", "kind": "phrasal verb",
    "ex1": {"question": "q", "answer": "circle back"},
    "ex2": {"sentence": "s", "correct": "B", "options": {"A": "wrap up", "B": "circle back", "C": "push back"}},
}


@pytest.fixture
def client(monkeypatch):
    from interfaces.api import auth as auth_mod
    from interfaces.api.routers import english
    monkeypatch.setattr(auth_mod, "_emit_security_event", lambda *a, **k: None)
    monkeypatch.setattr(english, "_load_lesson", lambda lid: REC if lid == "EN-2026-10-08" else None)
    app = FastAPI()
    app.include_router(english.router)
    return TestClient(app)


def _auth():
    from config.settings import settings
    return {"Authorization": f"Bearer {settings.internal_api_secret}"}


def _post(client, quoted, answer, auth=True):
    return client.post("/api/english/answer", headers=_auth() if auth else {},
                       json={"phone": "130554506788994@lid", "quoted_text": quoted, "answer": answer})


def test_sin_secreto(client):
    assert _post(client, "🆔 EN-2026-10-08", "B", auth=False).status_code in (401, 403)


def test_califica_con_la_leccion_de_redis(client):
    r = _post(client, "*English of the day*…\n🆔 EN-2026-10-08", "1) circle back 2) B")
    assert r.status_code == 200 and "2/2" in r.json()["reply"]


def test_leccion_vencida(client):
    assert "anymore" in _post(client, "🆔 EN-2026-09-01", "B").json()["reply"]


def test_cita_sin_id(client):
    assert "Quote" in _post(client, "otro mensaje", "B").json()["reply"]
