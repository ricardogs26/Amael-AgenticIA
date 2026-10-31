from __future__ import annotations

import pytest

from agents.reception import commands, storage
from agents.reception.storage import Lead


def _lead(**kw) -> Lead:
    base = dict(id=7, phone="5215550002222", name="Juan", company="Acme", reason="RAG",
                status="open", message_count=4, notified_at=None)
    base.update(kw)
    return Lead(**base)


class TestParse:
    @pytest.mark.parametrize("cmd,esperado", [
        ("", ("list", None, "")),
        ("lista", ("list", None, "")),
        ("ayuda", ("help", None, "")),
        ("7", ("show", 7, "")),
        ("7 ver", ("show", 7, "")),
        ("7 aprobar", ("invalid", None, "")),
        ("7 rechazar", ("rechazar", 7, "")),
        ("7 conversar", ("conversar", 7, "")),
        ("7 responder hola, ¿nos vemos el jueves?", ("responder", 7, "hola, ¿nos vemos el jueves?")),
        ("7 responder", ("responder", 7, "")),
        ("siete aprobar", ("invalid", None, "")),
    ])
    def test_parse(self, cmd, esperado):
        assert commands.parse(cmd) == esperado


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(commands, "is_admin_phone", lambda p: p == "ADMIN")
    from observability.metrics import RECEPTION_LEADS_TOTAL  # noqa: F401 — existe


class TestDispatch:
    def test_no_admin(self, admin):
        assert commands.dispatch("7 conversar", "OTRO") == commands.NOT_AVAILABLE

    def test_lista_vacia(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "list_open", lambda limit=10: [])
        assert "Sin leads" in commands.dispatch("", "ADMIN")

    def test_lead_inexistente(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: None)
        assert "No existe" in commands.dispatch("99", "ADMIN")

    def test_responder_sin_texto(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: _lead())
        assert "Falta el texto" in commands.dispatch("7 responder", "ADMIN")

    def test_responder_envia_y_guarda(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: _lead())
        guardado = []
        enviado = []
        monkeypatch.setattr(storage, "add_message", lambda lid, r, c: guardado.append((lid, r, c)))
        import agents.reception.notify as notify
        monkeypatch.setattr(notify, "send_to_visitor", lambda p, t: enviado.append((p, t)))
        out = commands.dispatch("7 responder nos vemos el jueves", "ADMIN")
        assert enviado == [("5215550002222", "*Ricardo:* nos vemos el jueves")]
        assert guardado == [(7, "ricardo", "nos vemos el jueves")]
        assert "Enviado" in out

    def test_rechazar_silencia(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: _lead())
        estados = []
        silenciados = []
        monkeypatch.setattr(storage, "set_status", lambda i, s: estados.append(s))
        import agents.reception.receptionist as rc
        monkeypatch.setattr(rc, "silence", lambda p: silenciados.append(p))
        commands.dispatch("7 rechazar", "ADMIN")
        assert estados == ["rejected"] and silenciados == ["5215550002222"]

    def test_conversar_abre_el_privado_y_guarda_el_saludo(self, admin, monkeypatch):
        from agents.reception import notify, prompts
        estados, msgs, enviados = [], [], []
        monkeypatch.setattr(storage, "get", lambda i: _lead(status="private"))
        monkeypatch.setattr(storage, "set_status", lambda i, s: estados.append(s))
        monkeypatch.setattr(storage, "add_message", lambda i, r, c: msgs.append((r, c)))
        monkeypatch.setattr(notify, "send_to_visitor", lambda p, t: enviados.append(t))
        assert "ya le contesta" in commands.dispatch("7 conversar", "ADMIN")
        assert estados == ["open"]
        assert enviados == [prompts.REPLY_CONVERSAR]
        assert msgs == [("amael", prompts.REPLY_CONVERSAR)]

    @pytest.mark.parametrize("status", ["open", "approved", "rejected"])
    def test_conversar_solo_para_privados(self, admin, monkeypatch, status):
        monkeypatch.setattr(storage, "get", lambda i: _lead(status=status))
        monkeypatch.setattr(storage, "set_status", lambda i, s: pytest.fail("no debe cambiar"))
        assert "solo para privados" in commands.dispatch("7 conversar", "ADMIN")

    def test_aprobar_ya_no_existe_y_no_toca_el_lead(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: pytest.fail("no debe leer el lead"))
        monkeypatch.setattr(storage, "set_status", lambda i, s: pytest.fail("no debe cambiar"))
        out = commands.dispatch("7 aprobar", "ADMIN")
        assert "No entendí" in out and "aprobar" not in commands.HELP

    def test_show_incluye_historial(self, admin, monkeypatch):
        monkeypatch.setattr(storage, "get", lambda i: _lead())
        monkeypatch.setattr(storage, "recent_messages", lambda i, n=8: [("visitor", "hola"), ("amael", "qué tal")])
        out = commands.dispatch("7", "ADMIN")
        assert "Juan" in out and "👤 hola" in out and "🤖 qué tal" in out
