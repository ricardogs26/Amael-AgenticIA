"""`get_recent_emails`: el barrido de compromisos lee correos LEÍDOS O NO de
los últimos N días, incluye `updates` (ahí caen los recibos) y sigue sin
promociones ni redes."""
from __future__ import annotations

from unittest.mock import MagicMock

from agents.productivity import email_manager as em


def _service(ids: list[str]):
    svc = MagicMock()
    msgs = svc.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": i} for i in ids]}
    msgs.get.return_value.execute.side_effect = [
        {"id": i, "snippet": f"snippet {i}",
         "payload": {"headers": [{"name": "Subject", "value": f"asunto {i}"},
                                 {"name": "From", "value": f"{i}@x.com"},
                                 {"name": "Date", "value": "Wed, 14 Oct 2026"}]}}
        for i in ids
    ]
    return svc


def test_pide_recientes_sin_exigir_no_leidos_y_sin_promociones(monkeypatch):
    svc = _service(["a", "b"])
    monkeypatch.setattr(em, "_build_gmail_service", lambda creds: svc)

    res = em.get_recent_emails(object(), days=7, max_results=40)

    kwargs = svc.users.return_value.messages.return_value.list.call_args.kwargs
    assert "UNREAD" not in kwargs.get("labelIds", [])
    assert "newer_than:7d" in kwargs["q"]
    assert "-category:promotions" in kwargs["q"] and "-category:social" in kwargs["q"]
    assert "-category:updates" not in kwargs["q"]     # los recibos viven ahí
    assert kwargs["maxResults"] == 40
    assert [r["id"] for r in res] == ["a", "b"]
    assert res[0]["subject"] == "asunto a" and res[0]["snippet"] == "snippet a"


def test_token_revocado_se_propaga(monkeypatch):
    import pytest

    from agents.productivity.errors import GoogleAuthRevocada
    svc = MagicMock()
    svc.users.return_value.messages.return_value.list.return_value.execute.side_effect = \
        Exception("invalid_grant: Token has been expired or revoked")
    monkeypatch.setattr(em, "_build_gmail_service", lambda creds: svc)
    with pytest.raises(GoogleAuthRevocada):
        em.get_recent_emails(object())
