"""
Consolidador de memoria episódica — Fase B1 del plan Hermes.

El mismo patrón que `agents/sre/runbook_consolidator.py` (validado en
producción), aplicado a las colecciones `memory_{user}`: los episodios crudos
se sintetizan con el LLM del tier profundo en un conjunto pequeño de HECHOS
estables («prefiere respuestas cortas», «trabaja en infraestructura de
Kubernetes»), los hechos reemplazan a los episodios que resumen, y son lo que
la Fase B2 inyecta completo al prompt.

Por qué hace falta (medido 7-ago-2026, 22 episodios): la heurística de
importancia da piso 0.3 y el umbral de guardado es `< 0.3`, así que TODO turno
se persiste — 20 de 22 eran «hola» y «gracias». Zaphkiel guarda sin destilar;
recuperar por coseno sobre esa paja devuelve paja.

Corre diario a las 03:30 hora de México sobre `ollama-cpu` (tier deep), media
hora después del consolidador de runbooks para no encimarse con él en el CPU.

La trampa heredada que NO hay que repetir: el nivel 3 de runbooks estuvo roto
desde su creación porque ningún add_job lo agendaba. El agendado vive en
`agents/scheduler/runner.py` y se loguea el next_run_time al arrancar.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("agents.memory.consolidator")

_QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")

# Mínimo de episodios para disparar una síntesis. Menos que eso no hay patrón
# que extraer y la llamada al LLM es puro costo.
MIN_EPISODES = int(os.environ.get("MEMORY_MIN_EPISODES_TO_CONSOLIDATE", "8"))
# Tope de episodios por corrida (tamaño de prompt). Los que no quepan hoy se
# consolidan mañana — el job es diario.
MAX_EPISODES_PER_RUN = int(os.environ.get("MEMORY_MAX_EPISODES_PER_RUN", "40"))
# Tope duro de hechos por usuario: el bloque de perfil (B2) se inyecta completo
# a cada prompt, así que su tamaño es presupuesto de contexto, no un detalle.
MAX_FACTS = int(os.environ.get("MEMORY_MAX_FACTS", "15"))
# El tier profundo corre en CPU (~16 tok/s, carga en frío ~26 s) — mismo
# criterio de timeout que el consolidador de runbooks.
_SYNTHESIS_TIMEOUT_S = int(os.environ.get("MEMORY_SYNTHESIS_TIMEOUT_S", "300"))

_SYSTEM = """Eres el consolidador de memoria de Amael. Recibes: (a) los hechos ya conocidos de un usuario y (b) episodios crudos de sus conversaciones recientes.

Devuelve SOLO un JSON: {"facts": [{"text": "...", "type": "preference|fact|project"}]}

Reglas:
- "facts" es el conjunto COMPLETO y actualizado: fusiona los hechos previos con lo nuevo. Si un episodio contradice un hecho previo, gana lo más reciente.
- Un hecho es algo ESTABLE del usuario: preferencias («prefiere respuestas cortas»), datos personales/laborales («trabaja en infraestructura de Kubernetes»), proyectos en curso. En tercera persona, una frase corta cada uno.
- La charla trivial (saludos, gracias, pruebas) NO produce hechos. Si los episodios son pura charla, devuelve los hechos previos sin cambios.
- Máximo {max_facts} hechos. Si hay más candidatos, conserva los más útiles para personalizar respuestas futuras.
- No inventes: cada hecho debe estar respaldado por los episodios o los hechos previos."""


# ── Qdrant helpers (HTTP directo, mismo estilo que agent_graph) ───────────────

def _qdrant(path: str, body: dict | None = None, timeout: float = 20.0,
            method: str | None = None):
    """
    Ojo con los verbos de Qdrant: el upsert de puntos es PUT /points — un POST
    a esa misma ruta es OTRO endpoint (batch por ids) y responde
    «missing field ids», que no dice nada de cuál fue el error real.
    """
    import urllib.request
    req = urllib.request.Request(
        f"{_QDRANT_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method or ("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp).get("result")


def _memory_collections() -> list[str]:
    cols = _qdrant("/collections") or {}
    return [c["name"] for c in cols.get("collections", [])
            if c["name"].startswith("memory_")]


def _scroll_all(collection: str) -> list[dict]:
    puntos, offset = [], None
    while True:
        body: dict[str, Any] = {"limit": 500, "with_payload": True,
                                "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        page = _qdrant(f"/collections/{collection}/points/scroll", body)
        if not page:
            break
        puntos += page.get("points", [])
        offset = page.get("next_page_offset")
        if offset is None:
            break
    return puntos


# ── Síntesis ──────────────────────────────────────────────────────────────────

def _synthesize(previos: list[str], episodios: list[str]) -> list[dict] | None:
    """LLM del tier profundo → lista de hechos, o None si algo falló."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.base.llm_factory import get_chat_llm

    llm = get_chat_llm(temperature=0.1, timeout=_SYNTHESIS_TIMEOUT_S, tier="deep")
    payload = {
        "hechos_previos": previos,
        "episodios": episodios,
    }
    resp = llm.invoke([
        SystemMessage(content=_SYSTEM.replace("{max_facts}", str(MAX_FACTS))),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ])
    texto = resp.content if isinstance(resp.content, str) else str(resp.content)
    # El tier profundo no siempre respeta format=json — extraer el objeto.
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin <= inicio:
        logger.warning(f"[memory.consolidator] Respuesta sin JSON: {texto[:120]!r}")
        return None
    try:
        data = json.loads(texto[inicio:fin + 1])
    except json.JSONDecodeError as exc:
        logger.warning(f"[memory.consolidator] JSON inválido: {exc}")
        return None

    hechos = []
    for f in (data.get("facts") or [])[:MAX_FACTS]:
        txt = str(f.get("text", "")).strip()
        tipo = str(f.get("type", "fact")).strip().lower()
        if txt and tipo in ("preference", "fact", "project"):
            hechos.append({"text": txt[:300], "type": tipo})
    return hechos


def _embed(texto: str) -> list[float] | None:
    try:
        from agents.base.llm_factory import get_embeddings
        return get_embeddings().embed_query(texto)
    except Exception as exc:
        logger.warning(f"[memory.consolidator] embedding falló: {exc}")
        return None


# ── Corrida ───────────────────────────────────────────────────────────────────

def consolidate_collection(collection: str) -> dict:
    """Consolida una colección. Devuelve el resumen de lo hecho (para logs/tests)."""
    puntos = _scroll_all(collection)
    hechos_previos = [p for p in puntos if (p.get("payload") or {}).get("kind") == "fact"]
    episodios      = [p for p in puntos if (p.get("payload") or {}).get("kind") != "fact"]

    if len(episodios) < MIN_EPISODES:
        return {"collection": collection, "skipped": True,
                "episodes": len(episodios), "reason": f"< {MIN_EPISODES} episodios"}

    lote = episodios[:MAX_EPISODES_PER_RUN]
    textos = [
        (p["payload"].get("content") or "")[:400].replace("\n", " ")
        for p in lote
    ]
    previos_txt = [p["payload"].get("text", "") for p in hechos_previos]

    hechos = _synthesize(previos_txt, textos)
    if hechos is None:
        return {"collection": collection, "skipped": True, "reason": "síntesis falló"}

    # Upsert de los hechos nuevos ANTES de borrar nada: si el upsert falla, los
    # episodios siguen ahí y mañana se reintenta. Perder episodios sin haber
    # persistido su destilado sería perder memoria de verdad.
    nuevos_ids = []
    ahora = datetime.now(UTC).isoformat()
    for h in hechos:
        vector = _embed(h["text"])
        if vector is None:
            return {"collection": collection, "skipped": True, "reason": "sin embeddings"}
        pid = str(uuid.uuid4())
        _qdrant(f"/collections/{collection}/points?wait=true", method="PUT", body={
            "points": [{
                "id": pid,
                "vector": vector,
                "payload": {
                    "kind":            "fact",
                    "text":            h["text"],
                    "fact_type":       h["type"],
                    "content":         h["text"],   # compat con retrieve/list
                    "episode_type":    h["type"],
                    "importance":      0.9 if h["type"] == "preference" else 0.7,
                    "timestamp":       ahora,
                    "consolidated_at": ahora,
                    "source_episodes": len(lote),
                },
            }],
        })
        nuevos_ids.append(pid)

    # Los hechos previos se REEMPLAZAN por el conjunto nuevo (el prompt pidió el
    # set completo fusionado) y los episodios consumidos se borran — mismo
    # contrato que el consolidador de runbooks.
    a_borrar = [p["id"] for p in hechos_previos] + [p["id"] for p in lote]
    if a_borrar:
        _qdrant(f"/collections/{collection}/points/delete?wait=true",
                {"points": a_borrar})

    try:
        from agents.memory_agent import profile
        profile.invalidate_for_collection(collection)
    except Exception:
        pass

    return {
        "collection": collection,
        "facts": len(nuevos_ids),
        "episodes_consumed": len(lote),
        "previous_facts_replaced": len(hechos_previos),
    }


def run_consolidation() -> list[dict]:
    """Consolida todas las colecciones de memoria. Punto de entrada del cron."""
    resultados = []
    try:
        cols = _memory_collections()
    except Exception as exc:
        logger.error(f"[memory.consolidator] Qdrant no disponible: {exc}")
        return resultados
    for col in cols:
        try:
            r = consolidate_collection(col)
        except Exception as exc:
            logger.error(f"[memory.consolidator] {col} falló: {exc}")
            r = {"collection": col, "skipped": True, "reason": str(exc)}
        logger.info(f"[memory.consolidator] {r}")
        resultados.append(r)
    return resultados
