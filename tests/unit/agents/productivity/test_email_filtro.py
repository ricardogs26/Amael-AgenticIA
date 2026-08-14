"""El brief no debe planificar el día a partir de correo promocional.

Caso real (14-ago-2026): los 10 «no leídos» del usuario eran Medium,
Aeroméxico, Reddit, Disney+, Cinépolis ×2, Pinterest, Skyscanner, Mozilla y un
boletín de IA. El prompt ordena «priorizar emails urgentes que requieran
respuesta hoy», así que el LLM obedeció y agendó ~4 h de trabajo: «parcheo del
CVE-2026-43499» salido de un post de Reddit sobre rootear un Galaxy S22.
El filtro es de Gmail, no nuestro: las categorías ya clasifican esto.
"""
from unittest.mock import MagicMock, patch

from agents.productivity import email_manager


def _servicio_vacio():
    servicio = MagicMock()
    servicio.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    return servicio


def test_se_excluyen_promociones_sociales_y_foros():
    servicio = _servicio_vacio()
    with patch.object(email_manager, "_build_gmail_service", return_value=servicio):
        email_manager.get_unread_emails(MagicMock())

    kwargs = servicio.users.return_value.messages.return_value.list.call_args.kwargs
    q = kwargs.get("q", "")
    for categoria in ("promotions", "social", "forums", "updates"):
        assert f"-category:{categoria}" in q, f"falta excluir {categoria}: {q!r}"


def test_el_filtro_se_puede_apagar():
    """Quien quiera el inbox completo (una búsqueda explícita del usuario, no el
    brief) debe poder pedirlo."""
    servicio = _servicio_vacio()
    with patch.object(email_manager, "_build_gmail_service", return_value=servicio):
        email_manager.get_unread_emails(MagicMock(), solo_relevantes=False)

    kwargs = servicio.users.return_value.messages.return_value.list.call_args.kwargs
    assert "-category:promotions" not in kwargs.get("q", "")


def test_sigue_pidiendo_solo_no_leidos_del_inbox():
    servicio = _servicio_vacio()
    with patch.object(email_manager, "_build_gmail_service", return_value=servicio):
        email_manager.get_unread_emails(MagicMock())

    kwargs = servicio.users.return_value.messages.return_value.list.call_args.kwargs
    assert kwargs["labelIds"] == ["INBOX", "UNREAD"]
