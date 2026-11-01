"""Credenciales de Qdrant (F1 del RAG gobernado, 1-nov-2026).

Qdrant exige `api-key` desde que corre con QDRANT__SERVICE__API_KEY. Todos los clientes del
repo (QdrantClient, LangChain y las llamadas urllib crudas) pasan por aquí para que la llave
viva en un solo lugar: la variable de entorno QDRANT_API_KEY, montada desde el Secret
`qdrant-auth`. Sin la variable (tests, desarrollo local contra un Qdrant abierto) no se
manda cabecera y todo sigue funcionando.
"""
from __future__ import annotations

import os


def qdrant_api_key() -> str | None:
    """Llave de Qdrant o None si no está configurada."""
    key = os.environ.get("QDRANT_API_KEY", "").strip()
    return key or None


def qdrant_headers(extra: dict | None = None) -> dict:
    """Cabeceras para llamadas HTTP crudas a Qdrant (añade `api-key` si hay llave)."""
    headers = dict(extra or {})
    key = qdrant_api_key()
    if key:
        headers["api-key"] = key
    return headers
