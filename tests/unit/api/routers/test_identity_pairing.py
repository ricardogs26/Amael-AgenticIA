"""
Tests del DM pairing (/api/identity/pair-code y /pair).

Lo que importa: solo un admin genera códigos, el canje es de un solo uso
(GETDEL atómico), y un código inválido no filtra nada.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def setex(self, key, ttl, value):
        self.store[key] = value

    def getdel(self, key):
        return self.store.pop(key, None)


class _FakeCursor:
    """Cursor mínimo: registra los execute y responde el role configurado."""
    def __init__(self, role):
        self._role = role
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchone(self):
        return (self._role,) if self._role else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def entorno(monkeypatch):
    import interfaces.api.routers.identity as ident

    fake_redis = _FakeRedis()
    monkeypatch.setattr(ident, "_redis", lambda: fake_redis)

    estado = {"role": "admin", "cursor": None}

    def fake_get_connection():
        estado["cursor"] = _FakeCursor(estado["role"])
        return _FakeConn(estado["cursor"])

    import storage.postgres.client as pg
    monkeypatch.setattr(pg, "get_connection", fake_get_connection)

    app = FastAPI()
    app.include_router(ident.router)

    # Autenticación simulada: get_current_user → usuario fijo; el internal
    # secret se cortocircuita porque aquí se prueba la lógica, no el auth.
    from interfaces.api.auth import get_current_user, require_internal_secret
    app.dependency_overrides[get_current_user] = lambda: "admin@richardx.dev"
    app.dependency_overrides[require_internal_secret] = lambda: None

    return TestClient(app), fake_redis, estado


def test_admin_genera_codigo_con_ttl(entorno):
    client, fake_redis, _ = entorno
    resp = client.post("/api/identity/pair-code")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"].startswith("AMAEL-") and len(data["code"]) == 14
    assert data["expires_in"] == 3600
    assert f"pairing:{data['code']}" in fake_redis.store


def test_no_admin_no_genera(entorno):
    client, _, estado = entorno
    estado["role"] = "user"
    assert client.post("/api/identity/pair-code").status_code == 403


def test_canje_da_de_alta_y_es_de_un_solo_uso(entorno):
    client, fake_redis, estado = entorno
    fake_redis.store["pairing:AMAEL-DEADBEEF"] = json.dumps(
        {"role": "user", "created_by": "admin@richardx.dev"}
    )
    resp = client.post("/api/identity/pair",
                       json={"code": "amael-deadbeef", "phone": "5219991234567"})
    assert resp.status_code == 200
    assert resp.json() == {"canonical_user_id": "5219991234567", "role": "user"}
    # Se insertó perfil e identidad whatsapp
    sqls = " || ".join(estado["cursor"].executed)
    assert "INSERT INTO user_profile" in sqls
    assert "user_identities" in sqls and "whatsapp" in sqls
    # Segundo canje del mismo código → consumido
    resp2 = client.post("/api/identity/pair",
                        json={"code": "AMAEL-DEADBEEF", "phone": "5219990000000"})
    assert resp2.status_code == 404


def test_codigo_invalido_es_404(entorno):
    client, _, _ = entorno
    resp = client.post("/api/identity/pair",
                       json={"code": "AMAEL-NOEXISTE", "phone": "5219991234567"})
    assert resp.status_code == 404


def test_el_canje_exige_internal_secret():
    """Sin el override, /pair debe rechazar peticiones sin el secret."""
    import os

    # settings exige el entorno completo del pod (mismo idiom que
    # test_camael_router): valores dummy antes de que el auth los importe.
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret-" + "x" * 20)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")

    import interfaces.api.routers.identity as ident
    from interfaces.api.auth import get_current_user

    app = FastAPI()
    app.include_router(ident.router)
    app.dependency_overrides[get_current_user] = lambda: "x@y.com"
    client = TestClient(app)
    resp = client.post("/api/identity/pair",
                       json={"code": "AMAEL-DEADBEEF", "phone": "1"})
    assert resp.status_code in (401, 403)
