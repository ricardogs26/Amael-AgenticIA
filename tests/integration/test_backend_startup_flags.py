"""Verifica que CAMAEL_MODE=remote no carga agents/devops en el backend."""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch


def test_camael_mode_remote_skips_devops_registration():
    """Con CAMAEL_MODE=remote, el backend NO debe importar/registrar Camael."""
    # Limpiar imports previos
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("agents.devops"):
            del sys.modules[mod_name]

    env = {
        "CAMAEL_MODE": "remote",
        "AGENTS_MODE": "inprocess",
        "INTERNAL_API_SECRET": "x" * 32,
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
        "POSTGRES_PASSWORD": "test-pw",
        "MINIO_ACCESS_KEY": "test-ak",
        "MINIO_SECRET_KEY": "test-sk",
    }
    with patch.dict(os.environ, env, clear=False):
        # Force settings reload
        if "config.settings" in sys.modules:
            importlib.reload(sys.modules["config.settings"])

        # Import perezoso de la función gate
        from main import _should_register_devops_inprocess
        assert _should_register_devops_inprocess() is False


def test_camael_mode_inprocess_loads_devops():
    env = {
        "CAMAEL_MODE": "inprocess",
        "INTERNAL_API_SECRET": "x" * 32,
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
        "POSTGRES_PASSWORD": "test-pw",
        "MINIO_ACCESS_KEY": "test-ak",
        "MINIO_SECRET_KEY": "test-sk",
    }
    with patch.dict(os.environ, env, clear=False):
        # Force settings reload
        if "config.settings" in sys.modules:
            importlib.reload(sys.modules["config.settings"])

        from main import _should_register_devops_inprocess
        assert _should_register_devops_inprocess() is True
