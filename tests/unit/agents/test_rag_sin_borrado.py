"""Un error de lectura nunca borra la colección de un usuario."""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from agents.researcher import rag_retriever


@pytest.fixture
def vectorstore(monkeypatch):
    clase = MagicMock()
    mod = types.ModuleType("langchain_qdrant")
    mod.QdrantVectorStore = clase
    monkeypatch.setitem(sys.modules, "langchain_qdrant", mod)
    return clase


def _usar(cliente):
    return (patch.object(rag_retriever, "_get_qdrant_client", return_value=cliente),
            patch.object(rag_retriever, "_get_embeddings", return_value=MagicMock()))


def test_error_de_lectura_no_borra(vectorstore):
    vectorstore.side_effect = TimeoutError("qdrant lento")
    cliente = MagicMock()
    cliente.collection_exists.return_value = True
    a, b = _usar(cliente)
    with a, b, pytest.raises(TimeoutError):
        rag_retriever.get_user_vectorstore("ana@example.com")
    cliente.delete_collection.assert_not_called()
    cliente.create_collection.assert_not_called()


def test_si_no_se_puede_saber_si_existe_no_crea(vectorstore):
    cliente = MagicMock()
    cliente.collection_exists.side_effect = ConnectionError("sin red")
    a, b = _usar(cliente)
    with a, b, pytest.raises(ConnectionError):
        rag_retriever.get_user_vectorstore("ana@example.com")
    cliente.delete_collection.assert_not_called()
    cliente.create_collection.assert_not_called()


def test_coleccion_nueva_se_crea(vectorstore):
    cliente = MagicMock()
    cliente.collection_exists.return_value = False
    a, b = _usar(cliente)
    with a, b:
        rag_retriever.get_user_vectorstore("ana@example.com")
    cliente.create_collection.assert_called_once()
    cliente.delete_collection.assert_not_called()
    vectorstore.from_existing_collection.assert_not_called()
