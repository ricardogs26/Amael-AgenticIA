"""
Regresión: los call sites de Zaphkiel deben construir un AgentContext completo.

`AgentContext` exige user_id, conversation_id, request_id y llm. Los tres call
sites de memoria (chat retrieve, chat store, router /api/memory) pasaban solo
dos, así que lanzaban TypeError. En chat.py el `except Exception` lo degradaba a
un `logger.debug` y la memoria episódica llevaba caída en silencio; en el router
salía como 503.

Estos tests fallan si alguien vuelve a omitir un campo obligatorio.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.agent_base import AgentContext

_REQUIRED = {"user_id", "conversation_id", "request_id", "llm"}
_CALL_SITES = [
    "interfaces/api/routers/chat.py",
    "interfaces/api/routers/memory.py",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_agent_context_requires_four_fields():
    """Si estos dejan de ser obligatorios, los tests de abajo sobran."""
    params = inspect.signature(AgentContext).parameters
    sin_default = {
        name for name, p in params.items() if p.default is inspect.Parameter.empty
    }
    assert sin_default == _REQUIRED


@pytest.mark.parametrize("rel_path", _CALL_SITES)
def test_call_sites_pass_all_required_fields(rel_path: str):
    """
    Análisis estático: toda llamada a AgentContext(...) en estos módulos debe
    nombrar los cuatro campos obligatorios. Se hace sobre el AST y no
    importando el módulo porque los routers arrastran FastAPI y el pool de
    Postgres.
    """
    source = (_repo_root() / rel_path).read_text(encoding="utf-8")
    llamadas = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentContext"
    ]
    assert llamadas, f"{rel_path} ya no construye AgentContext — ¿se movió el call site?"

    for call in llamadas:
        pasados = {kw.arg for kw in call.keywords if kw.arg}
        faltantes = _REQUIRED - pasados
        assert not faltantes, (
            f"{rel_path}:{call.lineno} construye AgentContext sin {sorted(faltantes)}. "
            "Faltar un campo obligatorio lanza TypeError, y en chat.py se lo traga "
            "el except → la memoria se cae en silencio."
        )


def test_context_minimo_de_memoria_es_construible():
    """El contexto que usan los call sites de memoria debe instanciar sin error."""
    ctx = AgentContext(
        user_id="ricardo@example.com",
        conversation_id="",
        request_id="memory-retrieve",
        llm=None,
        via="memory",
    )
    assert ctx.caller is None      # None → la arista se registra como "user"
    assert ctx.via == "memory"
