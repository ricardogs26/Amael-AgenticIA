"""query_agent reintenta una vez cuando qwen3.5 emite una tool-call mal formada.

Caso real 2-oct-2026: «XML syntax error on line 4: element <function> closed
by </parameter> (status code: -1)» y el audio matutino lo leyó como falla del
cluster.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from agents.sre import agent as sre_agent

_PARSE_ERR = RuntimeError(
    "XML syntax error on line 4: element <function> closed by </parameter> "
    "(status code: -1)"
)


class FakeAgent:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, _payload):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"messages": [AIMessage(content=out)]}


def _patch(monkeypatch, fake):
    monkeypatch.setattr(sre_agent, "_get_langgraph_agent", lambda: fake)
    monkeypatch.setattr(sre_agent, "_is_vault_question", lambda q: False)


def test_reintenta_y_responde(monkeypatch):
    fake = FakeAgent([_PARSE_ERR, "Todos los pods están Running."])
    _patch(monkeypatch, fake)
    assert sre_agent.query_agent("estado del cluster") == "Todos los pods están Running."
    assert fake.calls == 2


def test_dos_fallos_no_suena_a_falla_del_cluster(monkeypatch):
    fake = FakeAgent([_PARSE_ERR, _PARSE_ERR])
    _patch(monkeypatch, fake)
    out = sre_agent.query_agent("estado del cluster")
    assert fake.calls == 2
    assert "NO indica un problema del cluster" in out


def test_otros_errores_no_se_reintentan(monkeypatch):
    fake = FakeAgent([TimeoutError("read timed out")])
    _patch(monkeypatch, fake)
    out = sre_agent.query_agent("estado del cluster")
    assert fake.calls == 1
    assert out.startswith("❌ Error ejecutando agente SRE")
