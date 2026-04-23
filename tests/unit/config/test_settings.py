"""Unit tests for config/settings.py — feature flags split."""
from __future__ import annotations

import os

# Campos obligatorios mínimos para poder importar config.settings durante
# la recolección de pytest. `config/__init__.py` hace `from config.settings
# import settings` a nivel de módulo, lo que ejecuta `get_settings()` y
# falla si los secrets no están presentes. Los seteamos ANTES de importar.
# No son el objeto del test — solo desbloquean la validación de pydantic.
_BOOTSTRAP_ENV = {
    "INTERNAL_API_SECRET": "x" * 32,
    "JWT_SECRET_KEY": "x" * 32,
    "SESSION_SECRET_KEY": "x" * 32,
    "POSTGRES_PASSWORD": "x",
    "MINIO_ACCESS_KEY": "x",
    "MINIO_SECRET_KEY": "x",
}
for _k, _v in _BOOTSTRAP_ENV.items():
    os.environ.setdefault(_k, _v)

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402


def test_camael_mode_default_is_inprocess():
    """CAMAEL_MODE debe defaultear a 'inprocess' para rollout seguro."""
    from config.settings import Settings
    with patch.dict(os.environ, _BOOTSTRAP_ENV, clear=True):
        s = Settings()
        assert s.camael_mode == "inprocess"


def test_camael_mode_independent_from_agents_mode():
    """AGENTS_MODE=remote no debe activar Camael remoto automáticamente."""
    from config.settings import Settings
    env = {**_BOOTSTRAP_ENV, "AGENTS_MODE": "remote"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.agents_mode == "remote"
        assert s.camael_mode == "inprocess"  # ← canary independiente


def test_camael_mode_can_be_set_remote():
    from config.settings import Settings
    env = {**_BOOTSTRAP_ENV, "CAMAEL_MODE": "remote"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.camael_mode == "remote"


def test_camael_mode_rejects_invalid_value():
    """Valor inválido debe fallar al construir Settings (Pydantic Literal)."""
    from config.settings import Settings
    env = {**_BOOTSTRAP_ENV, "CAMAEL_MODE": "banana"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(Exception):  # ValidationError de pydantic
            Settings()
