"""Una autorización de Google revocada no puede leerse como «no tienes nada».

Caso real (14-ago-2026): el refresh token del usuario quedó revocado. Google
responde `invalid_grant: Token has been expired or revoked`, el lector lo
capturaba y devolvía [], y el brief concluía «✅ ¡Tu día está libre!» con el
calendario lleno. Es el patrón del Vault sellado del 7-ago: el sistema no puede
leer y lo reporta como que no hay nada que leer.
"""
from unittest.mock import MagicMock, patch

import pytest

from agents.productivity import calendar_manager, email_manager
from agents.productivity.errors import GoogleAuthRevocada


def _servicio_que_falla(mensaje):
    servicio = MagicMock()
    servicio.events.return_value.list.return_value.execute.side_effect = Exception(mensaje)
    servicio.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
        Exception(mensaje)
    )
    return servicio


def test_calendario_con_token_revocado_avisa_en_vez_de_devolver_vacio():
    servicio = _servicio_que_falla("invalid_grant: Token has been expired or revoked.")
    with patch.object(calendar_manager, "_build_calendar_service", return_value=servicio):
        with pytest.raises(GoogleAuthRevocada):
            calendar_manager.get_todays_events(MagicMock())


def test_correo_con_token_revocado_avisa_en_vez_de_devolver_vacio():
    servicio = _servicio_que_falla("invalid_grant: Token has been expired or revoked.")
    with patch.object(email_manager, "_build_gmail_service", return_value=servicio):
        with pytest.raises(GoogleAuthRevocada):
            email_manager.get_unread_emails(MagicMock())


def test_un_fallo_pasajero_de_la_api_sigue_devolviendo_vacio():
    """Solo la credencial revocada es una mentira peligrosa. Un 503 de Google
    no debe tumbar el brief entero."""
    servicio = _servicio_que_falla("503 Service Unavailable")
    with patch.object(calendar_manager, "_build_calendar_service", return_value=servicio):
        assert calendar_manager.get_todays_events(MagicMock()) == []


def test_se_reconoce_el_mensaje_de_google_en_cualquiera_de_sus_formas():
    for mensaje in [
        "invalid_grant: Token has been expired or revoked.",
        "('invalid_grant: Bad Request', {'error': 'invalid_grant'})",
        "RefreshError: invalid_grant",
    ]:
        assert calendar_manager._es_auth_revocada(Exception(mensaje)), mensaje
    assert not calendar_manager._es_auth_revocada(Exception("quota exceeded"))
