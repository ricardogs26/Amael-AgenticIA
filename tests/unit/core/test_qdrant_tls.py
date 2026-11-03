"""TLS hacia Qdrant con la CA interna (A35, nov-2026)."""
from __future__ import annotations

import ssl

from core import qdrant_auth as qa


def test_sin_ca_no_se_pasa_verify(monkeypatch):
    monkeypatch.delenv("QDRANT_CA_FILE", raising=False)
    monkeypatch.setenv("QDRANT_API_KEY", "llave")
    assert qa.qdrant_client_kwargs() == {"api_key": "llave"}
    assert qa.qdrant_ssl_context() is None


def test_con_ca_se_pasa_un_contexto_que_la_carga(monkeypatch):
    ruta = ssl.get_default_verify_paths().cafile or "/etc/ssl/certs/ca-certificates.crt"
    monkeypatch.setenv("QDRANT_CA_FILE", ruta)
    qa._ctx_cache.clear()
    kwargs = qa.qdrant_client_kwargs()
    assert isinstance(kwargs["verify"], ssl.SSLContext)
    assert kwargs["verify"] is qa.qdrant_ssl_context()  # se reutiliza
    qa._ctx_cache.clear()


def test_qdrant_client_acepta_el_contexto(monkeypatch):
    from qdrant_client import QdrantClient
    ctx = ssl.create_default_context()
    c = QdrantClient(url="https://qdrant.invalid:6333", api_key="x", verify=ctx,
                     check_compatibility=False)
    assert c._client._rest_args["verify"] is ctx
