"""
Tests: verificación de JWT en get_current_user (migración python-jose → PyJWT).

6-oct-2026: CVE-2026-85394 (python-jose ≤3.5.0, confusión de algoritmo, sin
fix) tumbó el CI en pip-audit. Se migró a PyJWT. Estos tests fijan el contrato:
los tokens HS256 ya emitidos (bot-amael, sesiones del front) siguen valiendo y
cualquier otro algoritmo —incluido `none`— se rechaza.
"""
from __future__ import annotations

import base64
import json
import os

# Env mínimo para que config.settings valide (mismo patrón que tests/contract).
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-" + "x" * 32)
os.environ.setdefault("JWT_SECRET_KEY",      "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("SESSION_SECRET_KEY",  "test-session-secret-" + "x" * 32)
os.environ.setdefault("POSTGRES_PASSWORD",   "test")
os.environ.setdefault("MINIO_ACCESS_KEY",    "test-minio-access-key")
os.environ.setdefault("MINIO_SECRET_KEY",    "test-minio-secret-key")

import jwt  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

from config.settings import settings  # noqa: E402


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _user(token: str) -> str:
    from interfaces.api.auth import get_current_user
    return get_current_user(_creds(token))


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_hs256_token_is_accepted():
    token = jwt.encode({"sub": "bot-amael@richardx.dev"}, settings.jwt_secret_key, algorithm="HS256")
    assert _user(token) == "bot-amael@richardx.dev"


def test_legacy_email_claim_still_works():
    token = jwt.encode({"email": "user@example.com"}, settings.jwt_secret_key, algorithm="HS256")
    assert _user(token) == "user@example.com"


def test_token_issued_by_login_endpoint_roundtrips():
    from interfaces.api.routers.auth import _create_jwt
    assert _user(_create_jwt("user@example.com")) == "user@example.com"


@pytest.mark.parametrize("token_factory", [
    lambda s: jwt.encode({"sub": "x@y.z"}, "otro-secreto-" + "z" * 32, algorithm="HS256"),
    lambda s: jwt.encode({"sub": "x@y.z"}, s, algorithm="HS512"),
    lambda s: f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'x@y.z'})}.",
    lambda s: jwt.encode({"foo": "bar"}, s, algorithm="HS256"),  # sin sub/email
    lambda s: "no-es-un-jwt",
], ids=["otro-secreto", "hs512", "alg-none", "sin-sub", "basura"])
def test_invalid_tokens_are_rejected(token_factory):
    with pytest.raises(HTTPException) as exc:
        _user(token_factory(settings.jwt_secret_key))
    assert exc.value.status_code == 401


def test_python_jose_is_not_imported_anywhere():
    import pathlib
    root = pathlib.Path(__file__).parents[2]
    offenders = [
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if ".venv" not in p.parts and "tests" not in p.parts
        and ("from jose" in p.read_text(errors="ignore") or "import jose" in p.read_text(errors="ignore"))
    ]
    assert offenders == []
