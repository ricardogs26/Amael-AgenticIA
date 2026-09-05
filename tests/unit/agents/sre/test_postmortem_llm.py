"""El postmortem se genera en el tier interactivo (GPU), no en el profundo.

Historia: el 5-ago-2026 (1.1.13) el postmortem se movió al 30b de CPU con un
cliente de 300 s, pero `_generate_postmortem_sync` esperaba el resultado con
`future.result(timeout=60)` escrito a mano. Generar en CPU tarda ~2 min, así
que TODOS los postmortems desde el 2-ago se descartaron (5 remediaciones
verificadas, 0 filas en sre_postmortems) y de paso cada viernes se cargaban
18 GB de RAM para nada. Decisión de Ricardo (4-sep-2026): el postmortem va al
9b local; el único timeout vive en una constante compartida.
"""
import concurrent.futures

import pytest

from agents.sre import reporter


@pytest.fixture(autouse=True)
def _reset_singleton():
    reporter._postmortem_llm = None
    yield
    reporter._postmortem_llm = None


def test_postmortem_usa_el_modelo_interactivo_aunque_exista_tier_profundo(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ollama_deep_url", "http://ollama-cpu-service:11434")
    monkeypatch.setattr(settings, "llm_model_deep", "qwen3:30b-a3b-instruct-2507-q4_K_M")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama-service:11434")
    monkeypatch.setattr(settings, "llm_model", "qwen3.5:9b")

    llm = reporter._get_postmortem_llm()

    assert llm.model == "qwen3.5:9b"
    assert llm.base_url == "http://ollama-service:11434"


def test_timeout_del_cliente_y_de_la_espera_son_el_mismo(monkeypatch):
    """Un cliente de 300 s con una espera de 60 s descarta trabajo terminado."""
    from config.settings import settings

    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama-service:11434")
    monkeypatch.setattr(settings, "llm_model", "qwen3.5:9b")

    llm = reporter._get_postmortem_llm()

    assert llm.client_kwargs["timeout"] == reporter.POSTMORTEM_TIMEOUT_S


def test_generate_sync_espera_hasta_la_constante_no_60s(monkeypatch):
    seen = {}

    class _Future:
        def result(self, timeout=None):
            seen["timeout"] = timeout
            raise concurrent.futures.TimeoutError()

    class _Executor:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, *a, **k): return _Future()

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _Executor)
    monkeypatch.setattr(reporter, "_get_postmortem_llm", lambda: type("L", (), {"invoke": lambda s, p: ""})())

    assert reporter._generate_postmortem_sync({"incident_key": "k"}) is None
    assert seen["timeout"] == reporter.POSTMORTEM_TIMEOUT_S
    assert reporter.POSTMORTEM_TIMEOUT_S >= 120
