"""El bot solo actúa a nombre de identidades activas; ante duda, rechaza."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from interfaces.api.auth import BOT_USER, identidad_activa, resolver_usuario_bot


def _conexion(filas):
    cur = MagicMock()
    cur.fetchone.side_effect = filas
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    return ctx


def test_usuario_activo_por_email():
    with patch("storage.postgres.client.get_connection", return_value=_conexion([(1,)])):
        assert identidad_activa("ana@example.com") is True


def test_identidad_alterna_activa():
    with patch("storage.postgres.client.get_connection", return_value=_conexion([None, (1,)])):
        assert identidad_activa("5215550000000") is True


def test_desconocido_no_esta_activo():
    with patch("storage.postgres.client.get_connection", return_value=_conexion([None, None])):
        assert identidad_activa("kb_vault_privado") is False


def test_falla_de_postgres_rechaza():
    with patch("storage.postgres.client.get_connection", side_effect=RuntimeError("caido")):
        assert identidad_activa("ana@example.com") is False


def test_bot_no_escribe_a_nombre_de_una_coleccion():
    with patch("interfaces.api.auth.identidad_activa", return_value=False):
        with pytest.raises(HTTPException) as e:
            resolver_usuario_bot(BOT_USER, "kb_publico")
    assert e.value.status_code == 403


def test_bot_actua_por_usuario_activo():
    with patch("interfaces.api.auth.identidad_activa", return_value=True):
        assert resolver_usuario_bot(BOT_USER, "ana@example.com") == "ana@example.com"


def test_usuario_normal_ignora_el_campo():
    with patch("interfaces.api.auth.identidad_activa") as activa:
        assert resolver_usuario_bot("ana@example.com", "otra@example.com") == "ana@example.com"
    activa.assert_not_called()


def test_bot_sin_usuario_se_queda_como_bot():
    assert resolver_usuario_bot(BOT_USER, None) == BOT_USER
