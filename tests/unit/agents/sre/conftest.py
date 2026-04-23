"""
Bootstrap env para tests de agents.sre que monkeypatchean clients.camael_client.

Importar `clients.camael_client` dispara `from config.settings import settings`,
que valida los secrets obligatorios (INTERNAL_API_SECRET, POSTGRES_PASSWORD,
MINIO_*). Si no están presentes, pytest falla en fase de colección.

Usamos `os.environ.setdefault()` para no pisar variables del shell del dev.
"""
from __future__ import annotations

import os

_BOOTSTRAP_ENV = {
    "INTERNAL_API_SECRET": "x" * 32,
    "JWT_SECRET_KEY":      "x" * 32,
    "SESSION_SECRET_KEY":  "x" * 32,
    "POSTGRES_PASSWORD":   "test-password",
    "MINIO_ACCESS_KEY":    "test-access",
    "MINIO_SECRET_KEY":    "test-secret",
}

for _k, _v in _BOOTSTRAP_ENV.items():
    os.environ.setdefault(_k, _v)
