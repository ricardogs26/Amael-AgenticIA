"""La ruta async no debe congelar el único worker con llamadas lentas."""
import ast
from pathlib import Path


def test_ingesta_async_no_llama_directo_al_llm_ni_a_embeddings():
    arbol = ast.parse(Path("interfaces/api/routers/ingest.py").read_text(encoding="utf-8"))
    ruta = next(n for n in ast.walk(arbol)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "ingest_document")
    directas = [n.func.attr for n in ast.walk(ruta)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "invoke" not in directas
    assert "add_documents" not in directas
