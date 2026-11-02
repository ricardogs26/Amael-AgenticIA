"""El gobierno del índice: quién queda en qué colección y qué nunca se sube."""
from pathlib import PurePosixPath

import pytest

from knowledge.vault_ingest import (
    Fragmento,
    Nota,
    cargar_config,
    debe_indexarse,
    filtrar_secretos,
    fragmentar,
    leer_nota,
    resolver_audiencia,
    separar_frontmatter,
    sincronizar,
)


@pytest.fixture(scope="module")
def cfg():
    return cargar_config()


@pytest.mark.parametrize(
    ("ruta", "frontmatter", "esperada"),
    [
        ("amael/a.md", {"audiencia": "sre"}, "sre"),
        ("amael/a.md", {"audiencia": "publico"}, "publico"),
        ("amael/a.md", {}, "privado"),
        ("amael/a.md", {"audiencia": "todos"}, "privado"),
        ("personal/a.md", {"audiencia": "publico"}, "privado"),
        ("trader/a.md", {"audiencia": "sre"}, "privado"),
        ("qwen/kanan/a.md", {"audiencia": "publico"}, "privado"),
    ],
)
def test_audiencia_efectiva(cfg, ruta, frontmatter, esperada):
    assert resolver_audiencia(PurePosixPath(ruta), frontmatter, cfg)[0] == esperada


def test_frontmatter_roto_es_privado(cfg, tmp_path):
    archivo = tmp_path / "a.md"
    archivo.write_text("---\naudiencia: [publico\n---\n# T\n\ntexto\n", encoding="utf-8")
    nota = leer_nota(archivo, PurePosixPath("amael/a.md"), cfg)
    assert nota.audiencia == "privado"
    assert nota.avisos


@pytest.mark.parametrize(
    ("ruta", "indexa"),
    [
        ("amael/a.md", True),
        ("inbox/a.md", False),
        ("scripts/a.md", False),
        ("amael/.obsidian/a.md", False),
        ("amael/llave.pem", False),
    ],
)
def test_exclusiones(cfg, ruta, indexa):
    assert debe_indexarse(PurePosixPath(ruta), cfg) is indexa


@pytest.mark.parametrize(
    "texto",
    [
        "token sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "AKIAABCDEFGHIJKLMNOP",
        "password: hunter2hunter2",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_secreto_no_pasa(cfg, texto):
    limpios, bloqueados, _ = filtrar_secretos([Fragmento(texto, "", 1)], cfg)
    assert limpios == []
    assert bloqueados


def test_informe_no_lleva_el_valor(cfg):
    secreto = "sk-abcdefghijklmnopqrstuvwx"
    _, bloqueados, _ = filtrar_secretos([Fragmento(secreto, "", 7)], cfg)
    assert secreto not in repr(bloqueados)


def test_fragmentos_respetan_el_tamano():
    cuerpo = "# Uno\n\n" + "\n\n".join(f"palabra {i} " * 40 for i in range(40))
    fragmentos = fragmentar(cuerpo, 800, 100)
    assert len(fragmentos) > 1
    assert all(len(f.texto) <= 800 * 4 + 2 for f in fragmentos)
    assert all(f.encabezado == "Uno" for f in fragmentos)


def test_encabezado_dentro_de_codigo_no_parte_seccion():
    fragmentos = fragmentar("# Uno\n\n```\n# comentario\n```\n", 800, 100)
    assert [f.encabezado for f in fragmentos] == ["Uno"]


def test_frontmatter_no_se_indexa():
    _, cuerpo, _ = separar_frontmatter("---\naudiencia: sre\n---\ncuerpo\n")
    assert cuerpo == "cuerpo\n"


class QdrantFalso:
    def __init__(self, indices):
        self.indices = indices
        self.ops = []

    def asegurar_coleccion(self, nombre):
        self.indices.setdefault(nombre, {})

    def indice(self, nombre):
        return dict(self.indices[nombre])

    def borrar_nota(self, coleccion, ruta):
        self.ops.append(("borrar", coleccion, ruta))

    def subir(self, coleccion, puntos):
        self.ops.append(("subir", coleccion, puntos[0]["payload"]["ruta"]))


def _nota(ruta, coleccion, hash_nota="h2", audiencia="privado"):
    return Nota(ruta, audiencia, coleccion, "T", [], hash_nota, [Fragmento("x", "", 1)])


@pytest.fixture
def sin_embeddings(monkeypatch):
    monkeypatch.setattr("knowledge.vault_ingest.embeber", lambda t, m: [[0.0]] * len(t))


def test_cambio_de_audiencia_borra_antes_de_subir(cfg, sin_embeddings):
    q = QdrantFalso({"kb_vault_sre": {"a.md": "h1"}})
    sincronizar([_nota("a.md", "kb_vault_privado")], q, cfg)
    assert q.ops.index(("borrar", "kb_vault_sre", "a.md")) < q.ops.index(
        ("subir", "kb_vault_privado", "a.md")
    )


def test_nota_sin_cambios_no_se_reprocesa(cfg, sin_embeddings):
    q = QdrantFalso({"kb_vault_privado": {"a.md": "h2"}})
    assert sincronizar([_nota("a.md", "kb_vault_privado")], q, cfg)["sin_cambio"] == 1
    assert q.ops == []


def test_nota_que_desaparece_sale_del_indice(cfg, sin_embeddings):
    q = QdrantFalso({"kb_vault_sre": {"vieja.md": "h1"}})
    sincronizar([_nota("a.md", "kb_vault_privado")], q, cfg)
    assert ("borrar", "kb_vault_sre", "vieja.md") in q.ops


def test_nota_que_queda_sin_fragmentos_sale_del_indice(cfg, sin_embeddings):
    nota = _nota("a.md", "kb_vault_sre")
    nota.fragmentos = []
    q = QdrantFalso({"kb_vault_sre": {"a.md": "h1"}})
    sincronizar([nota], q, cfg)
    assert ("borrar", "kb_vault_sre", "a.md") in q.ops


def test_freno_de_borrado_masivo(cfg, sin_embeddings):
    q = QdrantFalso({"kb_vault_sre": {f"{i}.md": "h" for i in range(20)}})
    with pytest.raises(SystemExit):
        sincronizar([_nota("a.md", "kb_vault_sre")], q, cfg)
    assert q.ops == []
