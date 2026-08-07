"""
Tests de la Fase B — destilación de memoria (B1) y bloque de perfil (B2).

Los invariantes que protegen memoria de verdad:
  - los hechos nuevos se persisten ANTES de borrar episodios: si el upsert
    falla, mañana se reintenta; borrar primero perdería memoria;
  - la charla trivial no produce hechos (medido: 20 de 22 episodios eran
    «hola»/«gracias» con importance 0.3);
  - el tope del bloque de perfil se aplica EN CÓDIGO, por líneas completas,
    con las preferencias primero.
"""
from __future__ import annotations

import json

import pytest

from agents.memory_agent import consolidator, profile

# ── B1: consolidación ─────────────────────────────────────────────────────────

def _punto(pid, payload):
    return {"id": pid, "payload": payload}


class _QdrantEspia:
    """Captura upserts y deletes en orden, sirve el scroll configurado."""

    def __init__(self, puntos):
        self.puntos = puntos
        self.operaciones: list[tuple[str, object]] = []

    def __call__(self, path, body=None, timeout=20.0, method=None):
        if path == "/collections":
            return {"collections": [{"name": "memory_u_at_x_dot_com"}]}
        if path.endswith("/points/scroll"):
            return {"points": self.puntos, "next_page_offset": None}
        if "/points/delete" in path:
            self.operaciones.append(("delete", body["points"]))
            return {}
        if "/points" in path:
            # Regresión del primer run real: el upsert de Qdrant es PUT; con
            # POST responde 400 «missing field ids». El espía lo exige.
            assert method == "PUT", f"upsert debe ser PUT, llegó {method!r}"
            self.operaciones.append(("upsert", body["points"][0]["payload"]))
            return {}
        return {}


@pytest.fixture
def episodios_reales():
    """Mezcla medida en producción: paja + dos preferencias reales."""
    eps = [_punto(f"e{i}", {"content": "Usuario: hola Amael: ¿En qué ayudo?",
                            "episode_type": "conversation", "importance": 0.3})
           for i in range(8)]
    eps.append(_punto("e-pref", {
        "content": "Usuario: prefiero respuestas cortas Amael: Entendido.",
        "episode_type": "preference", "importance": 0.7,
    }))
    return eps


def test_los_hechos_se_escriben_antes_de_borrar(monkeypatch, episodios_reales):
    espia = _QdrantEspia(episodios_reales)
    monkeypatch.setattr(consolidator, "_qdrant", espia)
    monkeypatch.setattr(consolidator, "_embed", lambda t: [0.1] * 8)
    monkeypatch.setattr(
        consolidator, "_synthesize",
        lambda previos, eps: [{"text": "Prefiere respuestas cortas",
                               "type": "preference"}],
    )

    r = consolidator.consolidate_collection("memory_u_at_x_dot_com")
    assert r["facts"] == 1
    assert r["episodes_consumed"] == 9

    tipos = [op for op, _ in espia.operaciones]
    assert tipos.index("upsert") < tipos.index("delete"), \
        "borró episodios antes de persistir el destilado"
    upsert = next(p for op, p in espia.operaciones if op == "upsert")
    assert upsert["kind"] == "fact"
    assert upsert["importance"] == 0.9   # preferencia > hecho


def test_si_la_sintesis_falla_no_se_borra_nada(monkeypatch, episodios_reales):
    espia = _QdrantEspia(episodios_reales)
    monkeypatch.setattr(consolidator, "_qdrant", espia)
    monkeypatch.setattr(consolidator, "_synthesize", lambda p, e: None)

    r = consolidator.consolidate_collection("memory_u_at_x_dot_com")
    assert r["skipped"] is True
    assert espia.operaciones == []


def test_sin_embeddings_no_se_borra_nada(monkeypatch, episodios_reales):
    """El gotcha de nomic-embed-text ausente: embeddings vacíos en silencio."""
    espia = _QdrantEspia(episodios_reales)
    monkeypatch.setattr(consolidator, "_qdrant", espia)
    monkeypatch.setattr(consolidator, "_embed", lambda t: None)
    monkeypatch.setattr(
        consolidator, "_synthesize",
        lambda p, e: [{"text": "x", "type": "fact"}],
    )
    r = consolidator.consolidate_collection("memory_u_at_x_dot_com")
    assert r["skipped"] is True
    assert not any(op == "delete" for op, _ in espia.operaciones)


def test_pocos_episodios_no_disparan_llm(monkeypatch):
    espia = _QdrantEspia([_punto("e1", {"content": "hola",
                                        "episode_type": "conversation"})])
    llamado = []
    monkeypatch.setattr(consolidator, "_qdrant", espia)
    monkeypatch.setattr(consolidator, "_synthesize",
                        lambda p, e: llamado.append(1) or [])
    r = consolidator.consolidate_collection("memory_u_at_x_dot_com")
    assert r["skipped"] is True and not llamado


def test_los_hechos_previos_se_reemplazan_no_se_duplican(monkeypatch):
    puntos = [
        _punto("f1", {"kind": "fact", "text": "Hecho viejo"}),
        *[_punto(f"e{i}", {"content": "Usuario: x Amael: y",
                           "episode_type": "conversation"}) for i in range(8)],
    ]
    espia = _QdrantEspia(puntos)
    monkeypatch.setattr(consolidator, "_qdrant", espia)
    monkeypatch.setattr(consolidator, "_embed", lambda t: [0.1] * 8)

    recibidos = {}

    def sintetiza(previos, eps):
        recibidos["previos"] = previos
        return [{"text": "Hecho fusionado", "type": "fact"}]

    monkeypatch.setattr(consolidator, "_synthesize", sintetiza)
    consolidator.consolidate_collection("memory_u_at_x_dot_com")

    # El LLM recibió los hechos previos para fusionar…
    assert recibidos["previos"] == ["Hecho viejo"]
    # …y el hecho viejo se borró junto con los episodios consumidos.
    borrados = next(ids for op, ids in espia.operaciones if op == "delete")
    assert "f1" in borrados


def test_extrae_json_aunque_el_tier_profundo_agregue_texto(monkeypatch):
    """El deep tier no siempre respeta format=json — se extrae el objeto."""
    class _Resp:
        content = 'Claro, aquí está:\n{"facts": [{"text": "T", "type": "fact"}]}\nSaludos'

    class _LLM:
        def invoke(self, msgs):
            return _Resp()

    import agents.base.llm_factory as fac
    monkeypatch.setattr(fac, "get_chat_llm", lambda **kw: _LLM())
    hechos = consolidator._synthesize([], ["ep"])
    assert hechos == [{"text": "T", "type": "fact"}]


# ── B2: bloque de perfil ──────────────────────────────────────────────────────

class _RedisFalso:
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def setex(self, k, ttl, v):
        self.store[k] = v

    def delete(self, k):
        self.store.pop(k, None)


@pytest.fixture
def redis_falso(monkeypatch):
    r = _RedisFalso()
    monkeypatch.setattr(profile, "_redis", lambda: r)
    return r


def test_preferencias_primero_y_tope_por_lineas(monkeypatch, redis_falso):
    hechos = [
        {"text": "Trabaja en infraestructura de Kubernetes", "fact_type": "fact",
         "importance": 0.7},
        {"text": "Prefiere respuestas cortas y directas", "fact_type": "preference",
         "importance": 0.9},
    ]
    monkeypatch.setattr(profile, "_fetch_facts", lambda u: hechos)
    bloque = profile.render_profile_block("u@x.com")
    lineas = bloque.splitlines()
    assert "Prefiere respuestas cortas" in lineas[1]
    assert len(bloque) <= profile.MAX_BLOCK_CHARS


def test_el_tope_corta_hechos_completos(monkeypatch, redis_falso):
    # 150 = header (57) + primera línea (72) + margen; la segunda ya no cabe.
    monkeypatch.setattr(profile, "MAX_BLOCK_CHARS", 150)
    hechos = [{"text": "x" * 70, "fact_type": "preference", "importance": 0.9},
              {"text": "y" * 70, "fact_type": "fact", "importance": 0.7}]
    monkeypatch.setattr(profile, "_fetch_facts", lambda u: hechos)
    bloque = profile.render_profile_block("u@x.com")
    assert "x" * 70 in bloque
    assert "y" not in bloque, "un hecho a medias es peor que un hecho menos"


def test_sin_hechos_no_hay_bloque_y_se_cachea_el_vacio(monkeypatch, redis_falso):
    llamadas = []
    monkeypatch.setattr(profile, "_fetch_facts",
                        lambda u: llamadas.append(1) or [])
    assert profile.render_profile_block("u@x.com") == ""
    assert profile.render_profile_block("u@x.com") == ""
    assert len(llamadas) == 1, "el caso vacío también se cachea"


def test_qdrant_caido_devuelve_vacio(monkeypatch, redis_falso):
    def explota(u):
        raise ConnectionError("qdrant down")
    monkeypatch.setattr(profile, "_fetch_facts", explota)
    assert profile.render_profile_block("u@x.com") == ""


def test_invalidate_borra_el_cache(monkeypatch, redis_falso):
    monkeypatch.setattr(
        profile, "_fetch_facts",
        lambda u: [{"text": "A", "fact_type": "fact", "importance": 0.5}],
    )
    profile.render_profile_block("u@x.com")
    assert redis_falso.store
    profile.invalidate("u@x.com")
    assert not redis_falso.store


def test_invalidate_por_coleccion_coincide_con_la_clave(monkeypatch, redis_falso):
    """El consolidador conoce la colección; ambas rutas deben tocar la misma clave."""
    monkeypatch.setattr(
        profile, "_fetch_facts",
        lambda u: [{"text": "A", "fact_type": "fact", "importance": 0.5}],
    )
    profile.render_profile_block("u@x.com")
    profile.invalidate_for_collection(profile.collection_for("u@x.com"))
    assert not redis_falso.store


def test_json_de_sintesis_respeta_el_esquema():
    """Tipos fuera del enum o textos vacíos no pasan a hechos."""
    class _Resp:
        content = json.dumps({"facts": [
            {"text": "válido", "type": "preference"},
            {"text": "", "type": "fact"},
            {"text": "tipo raro", "type": "opinion"},
        ]})

    class _LLM:
        def invoke(self, msgs):
            return _Resp()

    import pytest as _pytest

    import agents.base.llm_factory as fac
    mp = _pytest.MonkeyPatch()
    mp.setattr(fac, "get_chat_llm", lambda **kw: _LLM())
    try:
        hechos = consolidator._synthesize([], ["ep"])
    finally:
        mp.undo()
    assert hechos == [{"text": "válido", "type": "preference"}]
