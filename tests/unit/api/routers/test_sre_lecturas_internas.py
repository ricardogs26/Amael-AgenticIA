"""Lecturas del SRE: el backend llama a Raphael con el secreto interno (2-nov-2026)."""
import os
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

for _k, _v in {"INTERNAL_API_SECRET": "test-secret-" + "x" * 20, "JWT_SECRET_KEY": "x" * 32,
               "SESSION_SECRET_KEY": "x" * 32, "POSTGRES_PASSWORD": "test-pw",
               "MINIO_ACCESS_KEY": "test-ak", "MINIO_SECRET_KEY": "test-sk"}.items():
    os.environ.setdefault(_k, _v)

import config.settings  # noqa: E402,F401  (carga el módulo)
from interfaces.api.routers import sre  # noqa: E402

SECRETO = "secreto-de-prueba-interno"


def _settings():
    """El objeto vigente: otros tests recargan config.settings y dejan viejo un import directo."""
    return sys.modules["config.settings"].settings
RUTAS = ["/api/sre/incidents", "/api/sre/postmortems", "/api/sre/learning/stats",
         "/api/sre/slo/status", "/api/sre/maintenance"]


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(_settings(), "internal_api_secret", SECRETO)
    app = FastAPI()
    app.include_router(sre.router)
    falsos = {
        "get_recent_incidents": [], "get_recent_postmortems": [],
        "get_historical_success_rate": [], "get_slo_burn_rates": [],
        "load_slo_targets": [],
    }
    with patch.multiple("clients.raphael_client", **{k: (lambda *a, _v=v, **k: _v) for k, v in falsos.items()}):
        yield TestClient(app)


@pytest.mark.parametrize("ruta", RUTAS)
def test_secreto_interno_lee(cliente, ruta):
    r = cliente.get(ruta, headers={"Authorization": f"Bearer {SECRETO}"})
    assert r.status_code != 401 and r.status_code != 403, r.text


@pytest.mark.parametrize("ruta", RUTAS)
def test_sin_credenciales_401(cliente, ruta):
    assert cliente.get(ruta).status_code == 401


def test_secreto_equivocado_no_pasa(cliente):
    r = cliente.get("/api/sre/incidents", headers={"Authorization": "Bearer otro"})
    assert r.status_code == 401


def test_usuario_sin_rol_operator_403(cliente):
    with patch("interfaces.api.auth.get_current_user", return_value="alguien@x.com"), \
         patch("interfaces.api.auth.get_user_role", return_value="user"):
        r = cliente.get("/api/sre/incidents", headers={"Authorization": "Bearer jwt"})
    assert r.status_code == 403


def test_operator_lee(cliente):
    with patch("interfaces.api.auth.get_current_user", return_value="op@x.com"), \
         patch("interfaces.api.auth.get_user_role", return_value="operator"):
        r = cliente.get("/api/sre/incidents", headers={"Authorization": "Bearer jwt"})
    assert r.status_code == 200


def test_secreto_vacio_no_abre(cliente, monkeypatch):
    monkeypatch.setattr(_settings(), "internal_api_secret", "")
    assert cliente.get("/api/sre/incidents", headers={"Authorization": "Bearer "}).status_code == 401
