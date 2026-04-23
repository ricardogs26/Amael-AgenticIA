"""
Bootstrap env para tests de config/settings.py.

`config/__init__.py` evalúa `settings = get_settings()` al importarse, lo que
ejecuta la validación de pydantic-settings. Si los secrets obligatorios no
están presentes, cualquier `from config.settings import Settings` falla en la
fase de colección de pytest — antes incluso de llegar a los tests.

pytest importa este `conftest.py` antes de coleccionar los tests de este
directorio, así que setear los env vars aquí desbloquea los imports a nivel
de módulo en `test_settings.py`.

Usamos `os.environ.setdefault()` para que un desarrollador pueda correr
`CAMAEL_MODE=remote pytest tests/unit/config/...` y que su valor gane sobre
el default.
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
