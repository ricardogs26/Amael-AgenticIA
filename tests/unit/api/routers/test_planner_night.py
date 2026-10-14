"""`POST /api/planner/night`: dispara a mano el nocturno de pendientes
(compromisos del correo + análisis). Mismo secreto interno que /daily, para
probarlo el día del despliegue sin esperar a las 03:45."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env():
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret-" + "x" * 20)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")


def _app():
    from fastapi import FastAPI

    from interfaces.api.routers import planner
    app = FastAPI()
    app.include_router(planner.router)
    return app


def test_sin_secreto_es_401_o_403():
    r = TestClient(_app()).post("/api/planner/night")
    assert r.status_code in (401, 403)


def test_con_secreto_corre_el_nocturno_y_devuelve_resultados(monkeypatch):
    monkeypatch.setattr("agents.scheduler.night_analysis.run_night_analysis_sync",
                        lambda: [{"user": "u@x.com", "commitments": {"registrados": 2}}])
    # El singleton `settings` puede haberse creado en otro test con otro
    # secreto (dependencia de orden conocida del CI): se sustituye la
    # dependencia en vez de adivinar el valor.
    from interfaces.api.auth import require_internal_secret
    app = _app()
    app.dependency_overrides[require_internal_secret] = lambda: None
    r = TestClient(app).post("/api/planner/night")
    assert r.status_code == 200
    assert r.json()["processed"] == 1
    assert r.json()["results"][0]["commitments"]["registrados"] == 2
