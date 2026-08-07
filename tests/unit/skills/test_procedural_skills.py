"""
Tests de C1 — skills procedurales con gate humano.

El invariante que más importa: NO EXISTE camino por el que un agente cree una
skill activa. Todo lo que escribe un agente es `proposed`, invisible para el
retrieval, hasta que un humano la aprueba por /sre skill approve. Estos tests
fijan ese diseño para que ninguna «mejora» futura lo relaje.
"""
from __future__ import annotations

import pytest

from skills.procedural import manage, store

SKILL_VALIDA = """---
name: sre-crash-loop
description: Remediar pods en CrashLoopBackOff con causa de memoria
scope: sre
version: 1
---

## Cuándo usar
Pod en CrashLoopBackOff con OOMKilled en el último estado.

## Procedimiento
1. kubectl describe pod para confirmar la causa.
2. Subir el límite de memoria vía PR (handoff a Camael).

## Peligros comunes
No reiniciar en bucle: si el límite no cambia, el pod vuelve a morir.

## Verificación
kubectl get pod: Running con 0 restarts durante 10 minutos.
"""


class _QdrantMem:
    """Store Qdrant en memoria: soporta scroll con filtro, upsert PUT y delete."""

    def __init__(self):
        self.puntos: dict[str, dict] = {}

    def __call__(self, path, body=None, timeout=15.0, method=None):
        if path == "/collections":
            return {"collections": [{"name": store.COLLECTION}]}
        if path.endswith(f"/collections/{store.COLLECTION}") and method == "PUT":
            return {}
        if path.endswith("/points/scroll"):
            filtro = (body or {}).get("filter") or {}
            must = filtro.get("must", [])
            out = []
            for pid, p in self.puntos.items():
                if all(p["payload"].get(c["key"]) == c["match"]["value"] for c in must):
                    out.append({"id": pid, "payload": p["payload"]})
            return {"points": out[: (body or {}).get("limit", 100)]}
        if "/points/delete" in path:
            for pid in body["points"]:
                self.puntos.pop(pid, None)
            return {}
        if path.endswith("/points/query"):
            must = (body or {}).get("filter", {}).get("must", [])
            out = [{"payload": p["payload"]} for p in self.puntos.values()
                   if all(p["payload"].get(c["key"]) == c["match"]["value"]
                          for c in must)]
            return {"points": out[: (body or {}).get("limit", 10)]}
        if "/points" in path:
            assert method == "PUT", "el upsert de Qdrant es PUT (gotcha del 7-ago)"
            for pt in body["points"]:
                self.puntos[pt["id"]] = pt
            return {}
        return {}


@pytest.fixture
def qdrant(monkeypatch):
    q = _QdrantMem()
    monkeypatch.setattr(store, "_qdrant", q)
    monkeypatch.setattr(store, "_embed", lambda t: [0.1] * 8)
    return q


# ── Formato ───────────────────────────────────────────────────────────────────

def test_una_skill_valida_parsea():
    p = store.parse_skill_md(SKILL_VALIDA)
    assert p["name"] == "sre-crash-loop"
    assert set(store.REQUIRED_SECTIONS) <= set(p["sections"])


@pytest.mark.parametrize("mutacion,fragmento_error", [
    (lambda s: s.replace("## Peligros comunes\nNo reiniciar en bucle: si el límite no cambia, el pod vuelve a morir.\n", ""), "Faltan secciones"),
    (lambda s: s.replace("## Verificación", "## Verificacion extra"), "Faltan secciones"),
    (lambda s: s.replace("name: sre-crash-loop", "name: SRE CRASH"), "name inválido"),
    (lambda s: s.replace("---\nname", "name"), "frontmatter"),
    (lambda s: s.replace("description: Remediar pods en CrashLoopBackOff con causa de memoria\n", ""), "description"),
])
def test_formatos_invalidos_se_rechazan_con_mensaje_accionable(mutacion, fragmento_error):
    """Un procedimiento sin Peligros o sin Verificación no es conocimiento."""
    with pytest.raises(store.SkillFormatError, match=fragmento_error):
        store.parse_skill_md(mutacion(SKILL_VALIDA))


# ── El gate ───────────────────────────────────────────────────────────────────

def test_toda_escritura_de_agente_queda_como_propuesta(qdrant):
    r = manage.skill_manage(action="create", owner_agent="raphael",
                            skill_md=SKILL_VALIDA, notify=False)
    assert r["ok"]
    assert store.get_skill("sre-crash-loop", status="proposed")
    assert store.get_skill("sre-crash-loop", status="active") is None


def test_las_propuestas_no_salen_en_el_retrieval(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    assert store.search_active("CrashLoopBackOff OOMKilled") == [], \
        "una propuesta sin aprobar llegó al contexto de diagnóstico"


def test_aprobar_activa_y_entra_al_retrieval(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    store.approve("sre-crash-loop")
    activas = store.search_active("CrashLoopBackOff OOMKilled")
    assert len(activas) == 1
    assert activas[0]["name"] == "sre-crash-loop"


def test_manage_no_puede_activar():
    """El módulo del agente ni siquiera conoce approve: es diseño, no config."""
    import inspect
    fuente = inspect.getsource(manage)
    assert "approve" not in fuente.replace("skill approve", ""), \
        "manage.py importa/usa approve — el gate dejó de ser estructural"


def test_rechazar_elimina_la_propuesta(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    assert store.reject("sre-crash-loop") is True
    assert store.get_skill("sre-crash-loop") is None


def test_nueva_propuesta_reemplaza_a_la_pendiente(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    v2 = SKILL_VALIDA.replace("version: 1", "version: 2")
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=v2, notify=False)
    pendientes = store.list_skills(status="proposed")
    assert len(pendientes) == 1
    assert pendientes[0]["version"] == "2"


def test_aprobar_reemplaza_la_activa_anterior(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    store.approve("sre-crash-loop")
    v2 = SKILL_VALIDA.replace("version: 1", "version: 2")
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=v2, notify=False)
    r = store.approve("sre-crash-loop")
    assert r["replaced_active"] is True
    activas = [s for s in store.list_skills(status="active")
               if s["name"] == "sre-crash-loop"]
    assert len(activas) == 1 and activas[0]["version"] == "2"


# ── Patch ─────────────────────────────────────────────────────────────────────

def test_patch_exige_coincidencia_unica():
    with pytest.raises(store.SkillFormatError, match="no aparece"):
        store.patch_text("abc", "zzz", "yyy")
    with pytest.raises(store.SkillFormatError, match="2 veces"):
        store.patch_text("abab", "ab", "xy")
    assert store.patch_text("abc", "b", "X") == "aXc"


def test_patch_de_activa_produce_propuesta_sin_tocar_la_activa(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    store.approve("sre-crash-loop")
    r = manage.skill_manage(
        action="patch", owner_agent="raphael", name="sre-crash-loop",
        old="0 restarts durante 10 minutos",
        new="0 restarts durante 15 minutos", notify=False,
    )
    assert r["ok"]
    activa = store.get_skill("sre-crash-loop", status="active")
    assert "10 minutos" in activa["text"], "el patch tocó la activa sin aprobación"
    propuesta = store.get_skill("sre-crash-loop", status="proposed")
    assert "15 minutos" in propuesta["text"]


def test_errores_de_formato_vuelven_como_detail_no_como_excepcion(qdrant):
    r = manage.skill_manage(action="create", owner_agent="raphael",
                            skill_md="esto no es una skill", notify=False)
    assert r["ok"] is False
    assert "frontmatter" in r["detail"]


# ── Comandos WhatsApp ─────────────────────────────────────────────────────────

def test_flujo_completo_por_comandos(qdrant, monkeypatch):
    from agents.sre.commands import handle_command
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)

    listado = handle_command("skills")
    assert "sre-crash-loop" in listado and "Propuestas" in listado

    show = handle_command("skill show sre-crash-loop")
    assert "## Peligros comunes" in show

    ok = handle_command("skill approve sre-crash-loop")
    assert "aprobada" in ok
    assert store.get_skill("sre-crash-loop", status="active")

    assert "No hay propuesta" in handle_command("skill approve sre-crash-loop")


# ── C2: progressive disclosure ────────────────────────────────────────────────

def test_el_catalogo_es_una_linea_por_skill_activa(qdrant):
    manage.skill_manage(action="create", owner_agent="raphael",
                        skill_md=SKILL_VALIDA, notify=False)
    # Una propuesta NO sale en el catálogo…
    assert store.list_skills_brief() == ""
    store.approve("sre-crash-loop")
    catalogo = store.list_skills_brief()
    assert catalogo.startswith("- sre-crash-loop — ")
    assert "\n" not in catalogo          # una sola skill = una sola línea
    assert "## Procedimiento" not in catalogo, \
        "el catálogo llevó el cuerpo — eso es lo que C2 evita"


def test_el_catalogo_se_corta_por_lineas_completas(qdrant, monkeypatch):
    for i in range(15):
        md = SKILL_VALIDA.replace("sre-crash-loop", f"sre-skill-{i:02d}")
        manage.skill_manage(action="create", owner_agent="raphael",
                            skill_md=md, notify=False)
        store.approve(f"sre-skill-{i:02d}")
    catalogo = store.list_skills_brief()
    assert len(catalogo) <= 950
    assert "más" in catalogo.splitlines()[-1], \
        "el corte debe DECIR que hay más, no truncar en silencio"
