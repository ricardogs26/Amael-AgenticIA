"""Nombres de colección RAG: prefijo propio y sin colisiones."""
from unittest.mock import patch

import pytest

from agents.researcher.rag_retriever import rag_collection
from skills.rag.skill import RAGSkill, RetrieveInput


def test_lleva_prefijo_y_es_estable():
    n = rag_collection("Ana.Paz@example.com")
    assert n.startswith("rag_ana_paz_example_com_")
    assert n == rag_collection("ana.paz@example.com")


@pytest.mark.parametrize("a,b", [
    ("a.b@x.com", "a_dot_b@x.com"),
    ("a-b@x.com", "a_b@x.com"),
    ("a+b@x.com", "a.b@x.com"),
])
def test_ids_parecidos_no_colisionan(a, b):
    assert rag_collection(a) != rag_collection(b)


@pytest.mark.parametrize("uid", ["kb_publico", "kb_vault_privado", "memory_x", "sre_runbooks"])
def test_nunca_coincide_con_otra_coleccion(uid):
    assert rag_collection(uid).startswith("rag_") and rag_collection(uid) != uid


def test_id_largo_cabe_en_un_nombre_valido():
    n = rag_collection("x" * 500 + "@example.com")
    assert len(n) < 80


@pytest.mark.asyncio
async def test_skill_sin_identidad_rechaza():
    salida = await RAGSkill().execute(RetrieveInput(query="q"))
    assert not salida.success


@pytest.mark.asyncio
async def test_skill_usa_su_identidad_y_no_acepta_otra():
    assert "user_email" not in RetrieveInput.model_fields
    with patch("agents.researcher.rag_retriever.retrieve_documents", return_value="doc") as r:
        await RAGSkill(user_email="ana@example.com").execute(RetrieveInput(query="q"))
    assert r.call_args.args[0] == "ana@example.com"


def test_el_contexto_liga_el_skill_a_su_usuario():
    from orchestration.context_factory import ContextFactory
    from skills import register_all_skills
    register_all_skills()
    skills = ContextFactory._load_skills(["rag"], "ana@example.com")
    assert skills["rag"].user_email == "ana@example.com"
