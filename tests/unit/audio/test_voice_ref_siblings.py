"""
La voz clonada es del USUARIO, no del identificador con el que llegó el mensaje.

7-oct-2026: el mensaje de Ricardo llegó con su @lid (130554506788994); su voz
está registrada bajo su número (5219993437008) y la respuesta salió con la voz
neutral.
"""
from __future__ import annotations

import pytest

from audio import voice_ref

_REFS = {"5219993437008": ("wavb64", "transcripción de Ricardo")}


@pytest.fixture
def refs(monkeypatch):
    monkeypatch.setattr(voice_ref, "_get_by_key", lambda safe: _REFS.get(safe))
    siblings = {"130554506788994": ["5219993437008"], "5219993437008": ["130554506788994"]}
    monkeypatch.setattr(voice_ref, "_sibling_identities", lambda safe: siblings.get(safe, []))


def test_por_el_numero_directo(refs):
    assert voice_ref.get_voice_reference("5219993437008") == _REFS["5219993437008"]


def test_por_el_lid_encuentra_la_voz_del_numero(refs):
    assert voice_ref.get_voice_reference("130554506788994@lid") == _REFS["5219993437008"]


def test_usuario_sin_voz_en_ninguna_identidad(refs):
    assert voice_ref.get_voice_reference("5215550001111") is None


def test_borrar_no_alcanza_la_voz_de_otra_identidad(refs, monkeypatch):
    """delete_voice_reference(lid) no debe reportar como suya la voz del número."""
    borrados = []

    class _Minio:
        def remove_object(self, bucket, obj):
            borrados.append(obj)

    monkeypatch.setattr(voice_ref, "_get_minio", lambda: _Minio())
    import storage.redis.client as rc
    monkeypatch.setattr(rc, "get_redis_client", lambda: type("R", (), {"delete": lambda s, k: None})())
    assert voice_ref.delete_voice_reference("130554506788994") is False
    assert all("5219993437008" not in b for b in borrados)
