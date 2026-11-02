"""La superficie que ve el modelo: rutas, secretos y tamaño de la respuesta."""
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from knowledge import mcp_server as m  # noqa: E402


def test_proyecto_no_puede_salir_de_la_carpeta_qwen(tmp_path):
    destino = m.ruta_nota("../../personal", "../../etc/passwd", tmp_path, date(2026, 11, 1))
    assert destino.is_relative_to(tmp_path / "qwen")
    assert destino.parent == tmp_path / "qwen" / "personal"


def test_no_pisa_una_nota_existente(tmp_path):
    primera = m.ruta_nota("kanan", "tema", tmp_path, date(2026, 11, 1))
    primera.parent.mkdir(parents=True)
    primera.write_text("x")
    assert m.ruta_nota("kanan", "tema", tmp_path, date(2026, 11, 1)) != primera


def test_ruta_vacia_se_rechaza(tmp_path):
    with pytest.raises(ValueError):
        m.ruta_nota("!!!", "tema", tmp_path)


def test_secreto_rechaza_la_nota(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "VAULT", tmp_path)
    respuesta = m.guardar_nota_trabajo("token ghp_abcdefghijklmnopqrstuvwxyz123456", "kanan", "x")
    assert respuesta.startswith("Rechazada")
    assert list(Path(tmp_path).rglob("*.md")) == []


def test_ambito_invalido():
    with pytest.raises(ValueError):
        m.colecciones_de("memory_ricardo")


def test_ambito_solo_llega_a_colecciones_kb():
    assert all(c.startswith("kb_") for c in m.colecciones_de("todo"))


def test_respuesta_acotada():
    resultados = [{"puntaje": 0.9, "ruta": f"{i}.md", "linea": 1, "texto": "x" * 3000} for i in range(9)]
    assert len(m.formatear(resultados)) <= m.MAX_RESPUESTA + 200
