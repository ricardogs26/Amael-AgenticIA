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


# ── TLS (desde nov-2026) ──────────────────────────────────────────────────────
# Qdrant sirve HTTPS con un certificado de la CA interna `amael-ca` (cert-manager).
# QDRANT_CA_FILE apunta al `ca.crt` montado; sin la variable se usan las CA del
# sistema (o http en tests y desarrollo).

_ctx_cache: dict[str, object] = {}


def qdrant_ssl_context():
    """SSLContext que confía en la CA interna, o None si no está configurada."""
    ruta = os.environ.get("QDRANT_CA_FILE", "").strip()
    if not ruta:
        return None
    if ruta not in _ctx_cache:
        import ssl
        _ctx_cache[ruta] = ssl.create_default_context(cafile=ruta)
    return _ctx_cache[ruta]


def qdrant_client_kwargs() -> dict:
    """Argumentos de autenticación y TLS para `QdrantClient(url=..., **qdrant_client_kwargs())`."""
    kwargs: dict = {"api_key": qdrant_api_key()}
    ctx = qdrant_ssl_context()
    if ctx is not None:
        kwargs["verify"] = ctx
    return kwargs


def qdrant_urlopen(req, timeout: float):
    """`urllib.request.urlopen` con la CA interna para llamadas crudas a Qdrant."""
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout, context=qdrant_ssl_context())  # nosec B310
