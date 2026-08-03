"""
Tests de los parámetros de ChatOllama / OllamaEmbeddings.

langchain_ollama declara extra="ignore", así que un kwarg con nombre equivocado
se descarta en silencio y el modelo se queda con su comportamiento por defecto.
Eso ocurrió con `think=False` (el campo es `reasoning`) y `request_timeout`
(el timeout va en `client_kwargs`): durante meses ninguno de los dos tuvo efecto.

Con reasoning por defecto, qwen3 razona e inserta los tags dentro del contenido;
combinado con format="json" el parseo falla y la llamada puede tardar minutos.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

# Archivos que construyen un ChatOllama para producción.
_CHAT_OLLAMA_SITES = [
    "agents/trader/analyzer.py",
    "agents/trader/chat.py",
    "agents/devops/camael_analyzer.py",
    "orchestration/fast_chat.py",
    "agents/planner/agent.py",
    "agents/base/llm_factory.py",
]


def _source(rel: str) -> str:
    return (_ROOT / rel).read_text()


@pytest.mark.parametrize("rel", _CHAT_OLLAMA_SITES)
def test_no_ignored_kwargs(rel):
    """Ni `think=` ni `request_timeout=`: langchain_ollama los descarta."""
    src = _source(rel)
    # Permitidos en comentarios/docstrings; prohibidos como kwargs.
    for bad in (r"^\s*think\s*=", r"^\s*request_timeout\s*="):
        offenders = [
            ln for ln in src.splitlines()
            if re.match(bad, ln) and not ln.strip().startswith("#")
        ]
        assert not offenders, f"{rel}: kwarg ignorado por langchain_ollama → {offenders}"


def test_chatollama_accepts_the_params_we_use():
    """Los nombres que usamos existen de verdad en la versión instalada."""
    from langchain_ollama import ChatOllama

    for field in ("reasoning", "client_kwargs", "num_gpu", "keep_alive", "format"):
        assert field in ChatOllama.model_fields, f"ChatOllama no acepta {field}"


def test_chatollama_silently_ignores_unknown_kwargs():
    """Documenta por qué esto se nos pasó: no hay error, solo silencio."""
    from langchain_ollama import ChatOllama

    assert ChatOllama.model_config.get("extra") == "ignore"
    llm = ChatOllama(model="x", think=False, request_timeout=99)
    assert not hasattr(llm, "think")


def test_trader_disables_reasoning_and_sets_timeout():
    src = _source("agents/trader/analyzer.py")
    assert "reasoning=False" in src
    assert 'client_kwargs={"timeout": TRADER_LLM_TIMEOUT_S}' in src


def test_trader_timeout_is_below_cycle_interval():
    """
    El timeout debe cortar antes de que empiece el siguiente ciclo.

    El intervalo desplegado es 600 s (TRADER_LOOP_INTERVAL en el ConfigMap);
    el default del código es 900 s. Se valida contra el más corto de los dos.
    Se lee del fuente para no importar el paquete del trader, que arrastra
    APScheduler y no está en el entorno de test.
    """
    src = _source("agents/trader/analyzer.py")
    m = re.search(r'TRADER_LLM_TIMEOUT_S = int\(os\.environ\.get\("TRADER_LLM_TIMEOUT", "(\d+)"\)\)', src)
    assert m, "no se encontró la definición de TRADER_LLM_TIMEOUT_S"
    assert 0 < int(m.group(1)) < 600


# ── Embeddings en CPU ────────────────────────────────────────────────────────

def test_embed_num_gpu_defaults_to_cpu(monkeypatch):
    monkeypatch.delenv("EMBED_NUM_GPU", raising=False)
    from core.embedding_opts import embed_num_gpu

    assert embed_num_gpu() == 0


def test_embed_num_gpu_is_overridable(monkeypatch):
    monkeypatch.setenv("EMBED_NUM_GPU", "-1")
    from core.embedding_opts import embed_num_gpu

    assert embed_num_gpu() == -1


def test_embed_num_gpu_survives_garbage(monkeypatch):
    monkeypatch.setenv("EMBED_NUM_GPU", "no-soy-un-numero")
    from core.embedding_opts import embed_num_gpu

    assert embed_num_gpu() == 0


def test_embed_payload_carries_options(monkeypatch):
    monkeypatch.delenv("EMBED_NUM_GPU", raising=False)
    from core.embedding_opts import embed_payload

    p = embed_payload("nomic-embed-text", "hola")
    assert p["model"] == "nomic-embed-text"
    assert p["prompt"] == "hola"
    assert p["options"]["num_gpu"] == 0


def test_direct_embedding_calls_use_the_helper():
    """Ningún POST a /api/embeddings debe armar el json a mano."""
    for rel in ("agents/sre/agent.py", "agents/memory_agent/agent.py"):
        src = _source(rel)
        if "/api/embeddings" not in src:
            continue
        assert "embed_payload(" in src, f"{rel} no usa embed_payload()"
        assert '"prompt":' not in src.split("/api/embeddings")[1][:400], (
            f"{rel}: sigue armando el payload de embeddings a mano"
        )
