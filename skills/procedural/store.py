"""
Store de skills procedurales — Fase C1 del plan Hermes.

Skills en formato SKILL.md (el estándar de Hermes/agentskills.io): frontmatter
YAML + cuatro secciones fijas. Viven en Qdrant (`agent_skills`) con dos
estados:

  proposed → escrita por un agente, INVISIBLE para el retrieval, esperando
             aprobación humana por WhatsApp
  active   → aprobada; entra al contexto de diagnóstico por búsqueda semántica

El ciclo de vida es deliberadamente asimétrico: PROPONER lo hace un agente,
ACTIVAR solo lo hace una persona. No existe función que cree una skill activa
directamente — ese es el gate del plan («no negociable» con un trader de
dinero real en el mismo cluster), y está en el diseño del módulo, no en un
flag que alguien pueda apagar.

El formato con secciones se VALIDA en código: una skill sin «Peligros» o sin
«Verificación» no se puede proponer. Si el LLM generó un procedimiento sin
decir cómo verificar que funcionó, eso no es conocimiento — es una corazonada
con formato.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime

logger = logging.getLogger("skills.procedural")

_QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
COLLECTION  = "agent_skills"
_VECTOR_DIM = 768   # nomic-embed-text

REQUIRED_SECTIONS = ("Cuándo usar", "Procedimiento", "Peligros comunes", "Verificación")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class SkillFormatError(ValueError):
    """SKILL.md que no cumple el contrato. El mensaje es apto para el agente."""


# ── Qdrant HTTP (mismo estilo que consolidator; upsert es PUT, no POST) ───────

def _qdrant(path: str, body: dict | None = None, timeout: float = 15.0,
            method: str | None = None):
    import urllib.request
    req = urllib.request.Request(
        f"{_QDRANT_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp).get("result")


def _embed(texto: str) -> list[float]:
    from agents.base.llm_factory import get_embeddings
    vector = get_embeddings().embed_query(texto)
    if not vector:
        # El gotcha documentado: sin nomic-embed-text los embeddings regresan
        # [] en silencio y el punto sería inservible para búsqueda.
        raise SkillFormatError("embeddings no disponibles (¿nomic-embed-text?)")
    return vector


def ensure_collection() -> None:
    try:
        existentes = {c["name"] for c in (_qdrant("/collections") or {}).get("collections", [])}
        if COLLECTION not in existentes:
            _qdrant(f"/collections/{COLLECTION}", method="PUT", body={
                "vectors": {"size": _VECTOR_DIM, "distance": "Cosine"},
            })
            logger.info(f"[skills] Colección {COLLECTION} creada.")
    except Exception as exc:
        logger.warning(f"[skills] ensure_collection: {exc}")


# ── SKILL.md parse / render ───────────────────────────────────────────────────

def parse_skill_md(texto: str) -> dict:
    """
    Valida y descompone un SKILL.md. Lanza SkillFormatError con mensaje
    accionable — es lo que el agente (o el LLM) recibe para corregir.
    """
    texto = (texto or "").strip()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", texto, re.DOTALL)
    if not m:
        raise SkillFormatError(
            "SKILL.md debe abrir con frontmatter YAML delimitado por '---'."
        )
    fm_raw, cuerpo = m.groups()

    # Frontmatter plano clave: valor — sin YAML anidado, a propósito: menos
    # superficie de parseo para texto que escribe un LLM.
    fm: dict[str, str] = {}
    for linea in fm_raw.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if ":" not in linea:
            raise SkillFormatError(f"Línea de frontmatter sin ':': {linea!r}")
        k, v = linea.split(":", 1)
        fm[k.strip()] = v.strip().strip("'\"")

    name = fm.get("name", "")
    if not _NAME_RE.match(name):
        raise SkillFormatError(
            f"name inválido: {name!r} (kebab-case, 3-64 chars, ej. 'sre-crash-loop')."
        )
    if not fm.get("description"):
        raise SkillFormatError("frontmatter sin 'description'.")

    secciones: dict[str, str] = {}
    partes = re.split(r"^##\s+", cuerpo, flags=re.MULTILINE)
    for parte in partes[1:]:
        titulo, _, contenido = parte.partition("\n")
        secciones[titulo.strip()] = contenido.strip()

    faltantes = [s for s in REQUIRED_SECTIONS if not secciones.get(s)]
    if faltantes:
        raise SkillFormatError(
            f"Faltan secciones obligatorias con contenido: {faltantes}. "
            f"Un procedimiento sin 'Peligros comunes' y 'Verificación' no se acepta."
        )

    return {
        "name":        name,
        "description": fm["description"],
        "scope":       fm.get("scope", "sre"),
        "version":     fm.get("version", "1"),
        "sections":    secciones,
        "text":        texto,
    }


# ── Lectura ───────────────────────────────────────────────────────────────────

def _scroll(filtro: dict, limit: int = 100) -> list[dict]:
    try:
        page = _qdrant(f"/collections/{COLLECTION}/points/scroll", {
            "limit": limit, "with_payload": True, "with_vector": False,
            "filter": filtro,
        }) or {}
    except Exception as exc:
        # Incluye el 404 de colección aún inexistente (primer arranque): un
        # store vacío se comporta como vacío, no como excepción.
        logger.debug(f"[skills] scroll: {exc}")
        return []
    return page.get("points", [])


def list_skills(status: str | None = None, scope: str | None = None) -> list[dict]:
    must = []
    if status:
        must.append({"key": "status", "match": {"value": status}})
    if scope:
        must.append({"key": "scope", "match": {"value": scope}})
    try:
        return [
            {"id": p["id"], **(p.get("payload") or {})}
            for p in _scroll({"must": must} if must else {})
        ]
    except Exception as exc:
        logger.warning(f"[skills] list falló: {exc}")
        return []


def get_skill(name: str, status: str | None = None) -> dict | None:
    must = [{"key": "name", "match": {"value": name}}]
    if status:
        must.append({"key": "status", "match": {"value": status}})
    puntos = _scroll({"must": must}, limit=5)
    if not puntos:
        return None
    p = puntos[0]
    return {"id": p["id"], **(p.get("payload") or {})}


def search_active(query: str, scope: str = "sre", k: int = 2) -> list[dict]:
    """Skills ACTIVAS relevantes al query. Las propuestas jamás salen de aquí."""
    try:
        vector = _embed(query)
        res = _qdrant(f"/collections/{COLLECTION}/points/query", {
            "query": vector,
            "limit": k,
            "with_payload": True,
            "filter": {"must": [
                {"key": "status", "match": {"value": "active"}},
                {"key": "scope",  "match": {"value": scope}},
            ]},
        }) or {}
        return [p.get("payload") or {} for p in res.get("points", [])]
    except Exception as exc:
        logger.warning(f"[skills] search_active falló: {exc}")
        return []


# ── Escritura (siempre como propuesta) ────────────────────────────────────────

def propose(skill_md: str, owner_agent: str, reason: str = "") -> dict:
    """
    Registra una PROPUESTA. Único camino de entrada de una skill al store.

    Una propuesta pendiente del mismo nombre se reemplaza (la nueva versión del
    agente supera a la anterior sin acumular basura); una skill ACTIVA del
    mismo nombre no se toca — la propuesta convivirá con ella hasta que un
    humano decida.
    """
    ensure_collection()
    parsed = parse_skill_md(skill_md)     # lanza SkillFormatError si no cumple

    previa = get_skill(parsed["name"], status="proposed")
    if previa:
        _qdrant(f"/collections/{COLLECTION}/points/delete?wait=true",
                {"points": [previa["id"]]})

    vector = _embed(f"{parsed['description']}\n{parsed['sections']['Cuándo usar']}")
    pid = str(uuid.uuid4())
    _qdrant(f"/collections/{COLLECTION}/points?wait=true", method="PUT", body={
        "points": [{
            "id": pid,
            "vector": vector,
            "payload": {
                "name":        parsed["name"],
                "description": parsed["description"],
                "scope":       parsed["scope"],
                "version":     parsed["version"],
                "text":        parsed["text"],
                "status":      "proposed",
                "owner_agent": owner_agent,
                "reason":      reason[:400],
                "created_at":  datetime.now(UTC).isoformat(),
            },
        }],
    })
    logger.info(f"[skills] Propuesta registrada: {parsed['name']} (de {owner_agent})")
    return {"name": parsed["name"], "id": pid, "replaced_pending": bool(previa)}


def patch_text(original: str, old: str, new: str) -> str:
    """
    Semántica de patch de Hermes: `old` debe aparecer EXACTAMENTE UNA vez.
    Cero coincidencias o varias → error accionable, nunca adivinar cuál tocar.
    """
    n = original.count(old)
    if n == 0:
        raise SkillFormatError(
            "patch falló: el texto a reemplazar no aparece en la skill. "
            "Copia el fragmento EXACTO, incluidos espacios."
        )
    if n > 1:
        raise SkillFormatError(
            f"patch falló: el texto aparece {n} veces. Amplía el fragmento "
            f"hasta que sea único."
        )
    return original.replace(old, new, 1)


# ── Decisión humana ───────────────────────────────────────────────────────────

def approve(name: str) -> dict:
    """
    Activa una propuesta. La versión activa anterior del mismo nombre se
    retira — una skill vigente por nombre, mismo contrato que el consolidado
    de runbooks.
    """
    propuesta = get_skill(name, status="proposed")
    if not propuesta:
        raise SkillFormatError(f"No hay propuesta pendiente llamada {name!r}.")

    activa_previa = get_skill(name, status="active")

    # Activar re-upsertando el mismo punto con status nuevo, ANTES de retirar
    # la activa previa: el orden garantiza que nunca hay cero versiones.
    vector = _embed(
        f"{propuesta['description']}\n"
        f"{parse_skill_md(propuesta['text'])['sections']['Cuándo usar']}"
    )
    payload = {k: v for k, v in propuesta.items() if k != "id"}
    payload["status"]      = "active"
    payload["approved_at"] = datetime.now(UTC).isoformat()
    _qdrant(f"/collections/{COLLECTION}/points?wait=true", method="PUT", body={
        "points": [{"id": propuesta["id"], "vector": vector, "payload": payload}],
    })
    if activa_previa:
        _qdrant(f"/collections/{COLLECTION}/points/delete?wait=true",
                {"points": [activa_previa["id"]]})

    logger.info(f"[skills] Aprobada: {name} (reemplazó activa: {bool(activa_previa)})")
    return {"name": name, "replaced_active": bool(activa_previa)}


def reject(name: str) -> bool:
    propuesta = get_skill(name, status="proposed")
    if not propuesta:
        return False
    _qdrant(f"/collections/{COLLECTION}/points/delete?wait=true",
            {"points": [propuesta["id"]]})
    logger.info(f"[skills] Rechazada y eliminada: {name}")
    return True
