"""
Recepcionista: todo lo que decide está en código. Estos tests no tocan
Postgres, Redis ni Ollama.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agents.reception import prompts, receptionist, storage
from agents.reception.storage import Lead


def _lead(**kw) -> Lead:
    base = dict(id=1, phone="5215550001111", name=None, company=None, reason=None,
                status="open", message_count=0, notified_at=None)
    base.update(kw)
    return Lead(**base)


# ── parse_llm_json ────────────────────────────────────────────────────────────

class TestParse:
    def test_parcial(self):
        out = receptionist.parse_llm_json('{"reply":"Hola","name":"Juan","company":null}')
        assert out == {"reply": "Hola", "name": "Juan", "company": None, "reason": None}

    def test_json_invalido_respaldo(self):
        out = receptionist.parse_llm_json("no es json")
        assert out["reply"] == prompts.REPLY_FALLBACK and out["name"] is None

    def test_no_objeto_respaldo(self):
        assert receptionist.parse_llm_json("[1,2]")["reply"] == prompts.REPLY_FALLBACK

    def test_reply_larga_respaldo(self):
        raw = '{"reply":"' + "x" * 1001 + '"}'
        assert receptionist.parse_llm_json(raw)["reply"] == prompts.REPLY_FALLBACK

    def test_reply_vacia_respaldo(self):
        assert receptionist.parse_llm_json('{"reply":"  "}')["reply"] == prompts.REPLY_FALLBACK

    def test_campos_vacios_son_none(self):
        out = receptionist.parse_llm_json('{"reply":"ok","name":"   ","reason":""}')
        assert out["name"] is None and out["reason"] is None


# ── merge_fields ──────────────────────────────────────────────────────────────

class TestMerge:
    def test_no_pisa_existentes(self):
        lead = _lead(name="Juan")
        assert storage.merge_fields(lead, {"name": "Pedro", "company": "Acme"}) == {"company": "Acme"}

    def test_ignora_vacios(self):
        assert storage.merge_fields(_lead(), {"name": "", "company": None, "reason": "x"}) == {"reason": "x"}

    def test_trunca(self):
        assert len(storage.merge_fields(_lead(), {"reason": "y" * 500})["reason"]) == 200


# ── should_notify ─────────────────────────────────────────────────────────────

class TestNotify:
    def test_primer_aviso_al_completar(self):
        assert receptionist.should_notify(_lead(), True, 3) == "first"

    def test_sin_completar_no_avisa(self):
        assert receptionist.should_notify(_lead(), False, 4) is None

    def test_forzado_a_los_10(self):
        assert receptionist.should_notify(_lead(), False, 10) == "forced"

    def test_resumen_cada_5(self):
        lead = _lead(notified_at=datetime.now(UTC))
        assert receptionist.should_notify(lead, False, 5) == "summary"
        assert receptionist.should_notify(lead, False, 6) is None
        assert receptionist.should_notify(lead, False, 10) == "summary"


# ── Límites (Redis fake) ──────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.d: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
    def incr(self, k):
        self.d[k] = self.d.get(k, 0) + 1
        return self.d[k]
    def expire(self, k, s):
        self.ttl[k] = s
    def exists(self, k):
        return 1 if k in self.d else 0
    def setex(self, k, s, v):
        self.d[k] = v
        self.ttl[k] = s


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(receptionist, "_redis", lambda: r)
    monkeypatch.setattr(receptionist, "_limits", lambda: (3, 5))
    return r


class TestLimits:
    def test_tope_por_numero(self, fake_redis):
        for _ in range(3):
            assert receptionist.check_limits("A") is None
        assert receptionist.check_limits("A") == "phone"
        assert fake_redis.ttl["reception:rate:A"] == 86400

    def test_tope_global(self, fake_redis):
        for p in "ABCDE":
            assert receptionist.check_limits(p) is None
        assert receptionist.check_limits("F") == "global"

    def test_silence_y_is_silenced(self, fake_redis):
        assert receptionist.is_silenced("A") is False
        receptionist.silence("A")
        assert receptionist.is_silenced("A") is True
        assert fake_redis.ttl["reception:silenced:A"] == 30 * 86400


# ── handle_message (storage y LLM mockeados) ──────────────────────────────────

class TestTrigger:
    @pytest.mark.parametrize("txt", [
        "Hola Amael, vengo de richardx.dev",
        "hola amael vengo de richardx.dev",
        "HOLA AMAEL, VENGO DE RICHARDX.DEV",
        "Holá Amaél, vengo de richardx.dev!!",
        "Hola Amael, vengo de richardx.dev — quiero platicar de RAG",
    ])
    def test_acepta_variantes_del_enlace(self, txt):
        assert receptionist.is_trigger(txt)

    @pytest.mark.parametrize("txt", ["hola", "Hola Amael", "vengo de richardx.dev", "", "Soy Juan de Acme"])
    def test_rechaza_lo_demas(self, txt):
        assert not receptionist.is_trigger(txt)


class _Store:
    def __init__(self, lead):
        self.exists = False
        self.lead = lead
        self.msgs = []
        self.updates = {}
        self.count = lead.message_count
        self.notified = False
    def get_or_create(self, phone):
        self.exists = True
        return self.lead
    def get_by_phone(self, phone): return self.lead if self.exists else None
    def add_message(self, lid, role, content): self.msgs.append((role, content))
    def recent_messages(self, lid, n=8): return list(self.msgs)[-n:]
    def bump_count(self, lid):
        self.count += 1
        return self.count
    def update_fields(self, lid, **kw): self.updates.update(kw)
    def mark_notified(self, lid): self.notified = True


@pytest.fixture
def wired(monkeypatch, fake_redis):
    lead = _lead()
    st = _Store(lead)
    for name in ("get_or_create", "get_by_phone", "add_message", "recent_messages", "bump_count",
                 "update_fields", "mark_notified"):
        monkeypatch.setattr(storage, name, getattr(st, name))
    calls = {"llm": 0, "admin": []}
    def fake_llm(lead, history, text):
        calls["llm"] += 1
        return '{"reply":"Hola, soy Amael","name":"Juan","company":"Acme","reason":"RAG"}'
    monkeypatch.setattr(receptionist, "_ask_llm", fake_llm)
    import agents.reception.notify as notify
    monkeypatch.setattr(notify, "send_to_admin", lambda t: calls["admin"].append(t))
    return st, calls


class TestHandle:
    def test_sin_lead_y_sin_frase_aviso_privado(self, wired):
        st, calls = wired
        assert receptionist.handle_message("5215550001111", "hola, ¿quién eres?") == prompts.REPLY_PRIVATE
        assert calls["llm"] == 0 and st.msgs == [] and not st.exists

    def test_la_frase_abre_el_lead_y_luego_todo_pasa(self, wired):
        st, calls = wired
        assert receptionist.handle_message("5215550001111", "Hola Amael, vengo de richardx.dev") == "Hola, soy Amael"
        assert st.exists
        assert receptionist.handle_message("5215550001111", "soy Juan") == "Hola, soy Amael"
        assert calls["llm"] == 2

    def test_flujo_completo_avisa_una_vez(self, wired):
        st, calls = wired
        st.exists = True
        reply = receptionist.handle_message("5215550001111", "Hola, soy Juan de Acme, quiero RAG")
        assert reply == "Hola, soy Amael"
        assert st.updates == {"name": "Juan", "company": "Acme", "reason": "RAG"}
        assert [r for r, _ in st.msgs] == ["visitor", "amael"]
        assert len(calls["admin"]) == 1 and "#1" in calls["admin"][0]
        assert st.notified

    def test_silenciado_no_llama_llm(self, wired, fake_redis):
        st, calls = wired
        st.exists = True
        receptionist.silence("5215550001111")
        assert receptionist.handle_message("5215550001111", "hola") is None
        assert calls["llm"] == 0

    def test_rechazado_no_contesta(self, wired):
        st, calls = wired
        st.exists = True
        st.lead.status = "rejected"
        assert receptionist.handle_message("5215550001111", "hola") is None
        assert calls["llm"] == 0

    def test_media_sin_texto(self, wired):
        st, calls = wired
        st.exists = True
        assert receptionist.handle_message("5215550001111", "", has_media=True) == prompts.REPLY_MEDIA
        assert calls["llm"] == 0

    def test_tope_no_llama_llm(self, wired):
        st, calls = wired
        st.exists = True
        for _ in range(3):
            receptionist.handle_message("5215550001111", "hola")
        assert calls["llm"] == 3
        assert receptionist.handle_message("5215550001111", "hola") == prompts.REPLY_LIMIT
        assert calls["llm"] == 3

    def test_llm_caido_error_sin_decision(self, wired, monkeypatch):
        st, calls = wired
        st.exists = True
        def boom(*a): raise ConnectionError("ollama")
        monkeypatch.setattr(receptionist, "_ask_llm", boom)
        assert receptionist.handle_message("5215550001111", "hola") == prompts.REPLY_ERROR
        assert st.updates == {}

    def test_texto_se_trunca_a_500(self, wired):
        st, calls = wired
        st.exists = True
        receptionist.handle_message("5215550001111", "a" * 2000)
        assert len(st.msgs[0][1]) == 500


def test_prompt_no_cita_lo_prohibido():
    """Regla 1.17.2: el 9b recita lo que se le prohíbe."""
    s = prompts.build_system({})
    for palabra in ("no tengo esa función", "no puedo", "prohibido"):
        assert palabra not in s.lower()
    assert "Banco BASE" in s
