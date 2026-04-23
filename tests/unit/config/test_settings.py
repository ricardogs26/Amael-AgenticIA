"""Unit tests for config/settings.py — feature flags split."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from config.settings import Settings

# Env mínimos que los tests necesitan dentro de `patch.dict(..., clear=True)`.
# Debe coincidir con el bootstrap de `conftest.py` para que Settings() valide.
_REQUIRED_ENV = {
    "INTERNAL_API_SECRET": "x" * 32,
    "JWT_SECRET_KEY":      "x" * 32,
    "SESSION_SECRET_KEY":  "x" * 32,
    "POSTGRES_PASSWORD":   "test-password",
    "MINIO_ACCESS_KEY":    "test-access",
    "MINIO_SECRET_KEY":    "test-secret",
}


def test_camael_mode_default_is_inprocess():
    """CAMAEL_MODE debe defaultear a 'inprocess' para rollout seguro."""
    with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
        s = Settings()
        assert s.camael_mode == "inprocess"


def test_camael_mode_independent_from_agents_mode():
    """AGENTS_MODE=remote no debe activar Camael remoto automáticamente."""
    env = {**_REQUIRED_ENV, "AGENTS_MODE": "remote"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.agents_mode == "remote"
        assert s.camael_mode == "inprocess"  # ← canary independiente


def test_camael_mode_can_be_set_remote():
    env = {**_REQUIRED_ENV, "CAMAEL_MODE": "remote"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.camael_mode == "remote"


def test_camael_mode_rejects_invalid_value():
    """Valor inválido debe fallar la validación del Literal de pydantic."""
    env = {**_REQUIRED_ENV, "CAMAEL_MODE": "banana"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValidationError):
            Settings()
