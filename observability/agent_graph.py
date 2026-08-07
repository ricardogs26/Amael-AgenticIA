"""
Grafo de agentes — topología derivada del código + tráfico real de Prometheus.

El diagrama del dashboard #11 estaba escrito a mano en un panel de texto: se
desactualizaba en silencio cada vez que alguien registraba un agente. Aquí la
topología se deriva de las tres fuentes de verdad que ya existen, así que un
agente nuevo aparece en el grafo sin que nadie edite un diagrama:

  1. El grafo LangGraph compilado  → nodos y aristas del pipeline
  2. `AgentRegistry`               → agentes de dispatch directo y su metadata
  3. `required_skills` de cada uno → aristas agente → skill
  4. Qdrant                        → capa de conocimiento (runbooks y colecciones)

Encima de esa topología se pinta el tráfico medido (`amael_agent_edge_total`).
Una arista declarada pero con 0 invocaciones es información útil: significa
camino muerto, no error de dibujo.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

logger = logging.getLogger("observability.agent_graph")

# LangGraph nombra así a sus nodos sentinela. En el grafo los mostramos con los
# nombres que usa la instrumentación de `workflow_engine.py`.
_SENTINELS      = {"__start__": "user", "__end__": "end"}
_SENTINEL_KINDS = {"__start__": "entry", "__end__": "exit"}

# Las consultas son contadores crudos, sin ventana. Ver `_apply_traffic` para el
# porqué; el corolario grato es que ya no se concatena nada en la PromQL, así que
# desaparece la superficie de inyección que obligaba a una lista blanca.


_QDRANT_URL         = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
_RUNBOOKS_COLLECTION = "sre_runbooks"
# Tope del scroll de runbooks. Hoy son ~1300; el tope existe para que un
# crecimiento inesperado no convierta una carga de página en un escaneo de
# decenas de miles de puntos. Si se alcanza, el grafo lo dice en vez de mentir.
_MAX_RUNBOOK_POINTS = 20_000
_SCROLL_PAGE        = 1_000
# La capa de conocimiento es cara comparada con leer registries en memoria, y
# cambia en escala de horas (el consolidador corre 1 vez al día). Un TTL corto
# evita que abrir la página varias veces golpee Qdrant cada vez.
_KNOWLEDGE_TTL_S    = 60.0
_knowledge_cache: dict[str, Any] = {"t": 0.0, "data": None}


# ── Prometheus ────────────────────────────────────────────────────────────────

def _valor(serie: dict[str, Any]) -> float:
    """
    Valor de una serie, con NaN/Inf normalizados a 0.

    PromQL devuelve `NaN` cuando no hay muestras e `Inf` en divisiones por cero,
    y ninguno es JSON serializable: si se cuelan en la respuesta, FastAPI
    responde 500 «Out of range float values are not JSON compliant». Para una
    arista, «sin muestras» y «sin tráfico» son lo mismo, así que 0 es la
    lectura correcta (a diferencia de `error_rate`, donde sí se distingue).
    """
    import math

    try:
        valor = float(serie["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0
    return valor if math.isfinite(valor) else 0.0


def _query_vector(promql: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    """
    Query instantánea que devuelve TODAS las series, no solo la primera.

    `observability/slo.py:_query` colapsa el resultado a un escalar; aquí hacen
    falta las etiquetas de cada serie para saber qué arista es cuál.
    """
    try:
        import httpx

        from observability.slo import _prometheus_url

        resp = httpx.get(
            f"{_prometheus_url()}/api/v1/query",
            params={"query": promql},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("result", [])
    except Exception as exc:
        logger.debug(f"[graph] query falló ({promql}): {exc}")
        return []


# ── Topología ─────────────────────────────────────────────────────────────────

def _pipeline_topology() -> tuple[list[dict], list[dict]]:
    """Nodos y aristas del pipeline LangGraph, leídos del grafo compilado."""
    nodes: list[dict] = []
    edges: list[dict] = []
    try:
        from orchestration.workflow_engine import get_orchestrator

        graph = get_orchestrator().get_graph()
        for raw in graph.nodes:
            name = _SENTINELS.get(raw, raw)
            nodes.append({
                "id":    name,
                "label": name,
                "kind":  _SENTINEL_KINDS.get(raw, "pipeline"),
            })
        for edge in graph.edges:
            edges.append({
                "source": _SENTINELS.get(edge.source, edge.source),
                "target": _SENTINELS.get(edge.target, edge.target),
                "via":    "pipeline",
            })
    except Exception as exc:
        # Sin el pipeline el grafo sigue siendo útil: quedan los agentes de
        # dispatch directo, que son la mayoría.
        logger.warning(f"[graph] no se pudo leer el grafo LangGraph: {exc}")
    return nodes, edges


def _agent_topology() -> tuple[list[dict], list[dict]]:
    """Agentes registrados, sus skills declaradas y sus rutas de dispatch."""
    nodes: list[dict] = []
    edges: list[dict] = []
    try:
        from agents.base.agent_registry import AgentRegistry
        agentes = AgentRegistry.list_agents()
    except Exception as exc:
        logger.warning(f"[graph] AgentRegistry no disponible: {exc}")
        return nodes, edges

    skills_vistas: set[str] = set()
    for meta in agentes:
        name = meta.get("name")
        if not name:
            continue
        nodes.append({
            "id":           name,
            "label":        name,
            "kind":         "agent",
            "role":         meta.get("role", ""),
            "version":      meta.get("version", ""),
            "capabilities": meta.get("capabilities", []),
        })
        for skill in meta.get("required_skills", []) or []:
            if skill not in skills_vistas:
                skills_vistas.add(skill)
                nodes.append({"id": skill, "label": skill, "kind": "skill"})
            edges.append({"source": name, "target": skill, "via": "skill"})

    # Rutas de dispatch directo: intent → agente. Es un dict privado del
    # dispatcher, pero es la única fuente de verdad de qué agente atiende qué
    # intent; duplicar el mapa aquí lo dejaría desincronizado a la primera.
    try:
        from orchestration.agent_dispatcher import _DIRECT_DISPATCH
        for agente in set(_DIRECT_DISPATCH.values()):
            edges.append({"source": "user", "target": agente, "via": "dispatch"})
    except Exception as exc:
        logger.debug(f"[graph] rutas de dispatch no disponibles: {exc}")

    return nodes, edges


# ── Capa de conocimiento ──────────────────────────────────────────────────────

def _qdrant(path: str, body: dict | None = None, timeout: float = 12.0) -> dict | None:
    """GET/POST contra Qdrant. Best-effort: si no responde, la capa se omite."""
    try:
        req = urllib.request.Request(
            f"{_QDRANT_URL}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp).get("result")
    except Exception as exc:
        logger.debug(f"[graph] Qdrant {path} falló: {exc}")
        return None


def _clasificar_sin_tipo(payload: dict) -> str:
    """
    Agrupa los runbooks que no llevan `issue_type`.

    Son tres poblaciones con significados distintos, y meterlas en un solo cubo
    manda a perseguir problemas que no existen. Se distinguen por la firma de su
    payload, verificada contra la colección real el 6-ago-2026:

      {auto_generated: False, source, text}  → los .md de runbooks/ (18 de 18
          indexados). Base de conocimiento escrita a mano; no llevan issue_type
          por diseño y `init_runbooks_qdrant` los inventaría justamente por
          `source`.
      {bootstrapped, workload_*, image, ...} → nivel 2, generados al arrancar
          para describir los workloads del cluster.
      {content, file, name}                  → esquema viejo del k8s-agent
          retirado: usa `content` donde el actual usa `text`. Estos sí son
          huérfanos.

    Ojo con `file`: lo escribe el esquema legacy, no el actual. Clasificar por
    ese campo da exactamente el resultado contrario al correcto.
    """
    if payload.get("bootstrapped"):
        return "BOOTSTRAP_WORKLOADS"
    if payload.get("auto_generated") is False and payload.get("source"):
        return "BASE_ESTATICA"
    if payload.get("file") or payload.get("name"):
        return "LEGACY_K8S_AGENT"
    return "SIN_CLASIFICAR"


def _resumen_runbooks() -> tuple[dict[str, dict], bool]:
    """
    Agrupa los runbooks por `issue_type`, contando su procedencia.

    Se recorre con `scroll` pidiendo solo tres campos del payload y ningún
    vector: traer 1300 vectores de 768 dimensiones para contarlos sería absurdo.

    Devuelve `(resumen, completo)`; `completo=False` significa que se alcanzó el
    tope y los conteos son un piso, no el total — el front debe decirlo en vez de
    presentar un número incompleto como si fuera exacto.
    """
    resumen: dict[str, dict] = {}
    offset: Any = None
    vistos = 0

    while vistos < _MAX_RUNBOOK_POINTS:
        body: dict[str, Any] = {
            "limit": _SCROLL_PAGE,
            "with_vector": False,
            "with_payload": [
                "issue_type", "auto_generated", "consolidated",
                "source", "bootstrapped", "file",
            ],
        }
        if offset is not None:
            body["offset"] = offset
        page = _qdrant(f"/collections/{_RUNBOOKS_COLLECTION}/points/scroll", body)
        if not page:
            break
        for punto in page.get("points", []):
            payload = punto.get("payload") or {}
            tipo = payload.get("issue_type") or _clasificar_sin_tipo(payload)
            fila = resumen.setdefault(
                tipo, {"total": 0, "auto": 0, "consolidated": 0, "static": 0}
            )
            fila["total"] += 1
            if payload.get("consolidated"):
                fila["consolidated"] += 1
            elif payload.get("auto_generated"):
                fila["auto"] += 1
            else:
                fila["static"] += 1
            vistos += 1
        offset = page.get("next_page_offset")
        if offset is None:
            return resumen, True

    return resumen, offset is None


# Qué agente consume cada colección. El orden importa: `memory_` se evalúa antes
# que el caso general, que es «colección RAG de un usuario».
def _dueno_de_coleccion(nombre: str) -> tuple[str, str] | None:
    if nombre == _RUNBOOKS_COLLECTION:
        return None                       # se representa como nodos por issue_type
    if nombre.startswith("memory_"):
        return "zaphkiel", "memory"
    if "sre_knowledge" in nombre:
        return "raphael", "knowledge"
    return "sandalphon", "rag"


def _knowledge_topology() -> tuple[list[dict], list[dict], dict]:
    """Nodos y aristas de la capa de conocimiento: runbooks y colecciones RAG."""
    ahora = time.monotonic()
    if _knowledge_cache["data"] and ahora - _knowledge_cache["t"] < _KNOWLEDGE_TTL_S:
        return _knowledge_cache["data"]

    nodes: list[dict] = []
    edges: list[dict] = []
    meta: dict = {"available": False}

    colecciones = _qdrant("/collections")
    if not colecciones:
        return nodes, edges, meta
    meta["available"] = True

    for col in colecciones.get("collections", []):
        nombre = col.get("name", "")
        dueno  = _dueno_de_coleccion(nombre)
        if not dueno:
            continue
        info   = _qdrant(f"/collections/{nombre}") or {}
        agente, via = dueno
        nodes.append({
            "id":     f"qd:{nombre}",
            "label":  nombre,
            "kind":   "collection",
            "points": info.get("points_count"),
        })
        edges.append({"source": agente, "target": f"qd:{nombre}", "via": via})

    resumen, completo = _resumen_runbooks()
    meta["runbooks_complete"] = completo
    meta["runbooks_total"]    = sum(f["total"] for f in resumen.values())
    for tipo, fila in resumen.items():
        nodes.append({
            "id":       f"rb:{tipo}",
            "label":    tipo,
            "kind":     "runbook",
            "runbooks": fila,
        })
        edges.append({"source": "raphael", "target": f"rb:{tipo}", "via": "runbook"})

    _knowledge_cache["t"]    = ahora
    _knowledge_cache["data"] = (nodes, edges, meta)
    return nodes, edges, meta


def _dedupe_nodes(nodes: list[dict]) -> list[dict]:
    """Primer nodo gana: la metadata del agente pesa más que un nodo pelón."""
    out: dict[str, dict] = {}
    for node in nodes:
        out.setdefault(node["id"], node)
    return list(out.values())


def _dedupe_edges(edges: list[dict]) -> list[dict]:
    out: dict[tuple, dict] = {}
    for edge in edges:
        out.setdefault((edge["source"], edge["target"], edge["via"]), edge)
    return list(out.values())


# ── Tráfico ───────────────────────────────────────────────────────────────────

def _descubrir_aristas_medidas(
    nodes: list[dict], edges: list[dict], medidas: dict[tuple, float]
) -> None:
    """
    Agrega al grafo las aristas que Prometheus midió pero la topología no declara.

    Sin esto el grafo solo muestra lo que creemos que existe. Caso real: la
    llamada de `chat.py` a Zaphkiel (`user → zaphkiel` vía `memory`) no sale de
    `_DIRECT_DISPATCH` ni del grafo LangGraph, así que la arista se medía pero
    era invisible — el nodo mostraba 6 invocaciones y ninguna línea las
    explicaba.

    Se marcan con `discovered` para que el front pueda distinguirlas: una arista
    medida y no declarada es justo el hallazgo interesante — una conexión que el
    código hace y la topología no confiesa.
    """
    declaradas = {(e["source"], e["target"], e["via"]) for e in edges}
    conocidos  = {n["id"] for n in nodes}

    for (source, target, via), valor in medidas.items():
        if not source or not target or valor <= 0:
            continue
        if (source, target, via) in declaradas:
            continue
        for extremo in (source, target):
            if extremo not in conocidos:
                conocidos.add(extremo)
                nodes.append({"id": extremo, "label": extremo, "kind": "unknown"})
        edges.append({
            "source": source, "target": target, "via": via, "discovered": True,
        })


def _apply_traffic(nodes: list[dict], edges: list[dict]) -> bool:
    """
    Anota `invocations` en cada arista y en cada nodo, más `error_rate`.

    Se lee el contador crudo, sin `rate` ni `increase`. Las dos funciones miden
    variación DENTRO de una ventana, y en este laboratorio el tráfico llega en
    ráfagas cortas separadas por horas de silencio:

      - `rate[5m]` dio 0 en doce sondeos seguidos tras cinco turnos de chat
        reales (medido el 6-ago-2026), porque un promedio por segundo sobre una
        ráfaga se redondea a nada.
      - `increase[1h]` dio 0 con 24 muestras en la ventana, porque cada
        despliegue crea un pod nuevo: la serie arranca de cero y si la ráfaga
        ocurre antes del primer scrape, el contador ya está en su valor final
        cuando Prometheus lo ve por primera vez. Ninguna ventana lo recupera —
        el pod no existía antes.

    El contador acumulado responde «cuántas veces se ha recorrido este camino
    desde que arrancó el backend». Se reinicia en cada redespliegue, y ese es el
    precio: a cambio nunca marca cero solo porque el tráfico sea viejo.

    Devuelve False si Prometheus no respondió, para que el front distinga
    «sin tráfico» de «sin datos» — son cosas distintas y pintarlas igual haría
    que un backend caído se vea como un sistema ocioso.
    """
    por_arista = {
        (s["metric"].get("source"), s["metric"].get("target"), s["metric"].get("via")):
            _valor(s)
        for s in _query_vector(
            "sum by (source, target, via) (amael_agent_edge_total)"
        )
    }
    ejecuciones = _query_vector(
        "sum by (agent_name, success) (amael_agent_executions_total)"
    )
    if not por_arista and not ejecuciones:
        return False

    _descubrir_aristas_medidas(nodes, edges, por_arista)

    for edge in edges:
        edge["invocations"] = round(
            por_arista.get((edge["source"], edge["target"], edge["via"]), 0.0)
        )

    total: dict[str, float] = {}
    fallidas: dict[str, float] = {}
    for serie in ejecuciones:
        agente = serie["metric"].get("agent_name", "")
        valor  = _valor(serie)
        total[agente] = total.get(agente, 0.0) + valor
        if serie["metric"].get("success") == "false":
            fallidas[agente] = fallidas.get(agente, 0.0) + valor

    for node in nodes:
        ejec = total.get(node["id"])
        if ejec is None:
            continue
        node["invocations"] = round(ejec)
        # Sin invocaciones no hay tasa de error que reportar: 0/0 no es 0% de
        # error, es ausencia de dato. La división usa el valor sin redondear
        # para no inventar un 100% cuando ejec redondea a 0.
        node["error_rate"] = (
            round(fallidas.get(node["id"], 0.0) / ejec, 4) if ejec > 0 else None
        )
    return True


# ── API pública ───────────────────────────────────────────────────────────────

def build_graph(include_traffic: bool = True, include_knowledge: bool = True) -> dict:
    """Grafo completo listo para renderizar."""
    pipeline_nodes, pipeline_edges = _pipeline_topology()
    agent_nodes,    agent_edges    = _agent_topology()
    know_nodes, know_edges, know_meta = (
        _knowledge_topology() if include_knowledge else ([], [], {"available": False})
    )

    nodes = _dedupe_nodes(pipeline_nodes + agent_nodes + know_nodes)
    edges = _dedupe_edges(pipeline_edges + agent_edges + know_edges)

    has_traffic = _apply_traffic(nodes, edges) if include_traffic else False
    return {
        "nodes":       nodes,
        "edges":       edges,
        # Contrato explícito para el front: `invocations` es acumulado desde el
        # arranque del pod, no un promedio ni una ventana.
        "metric":      "cumulative_since_pod_start",
        "has_traffic": has_traffic,
        "knowledge":   know_meta,
        "counts": {
            "agents":     sum(1 for n in nodes if n["kind"] == "agent"),
            "pipeline":   sum(1 for n in nodes if n["kind"] == "pipeline"),
            "skills":     sum(1 for n in nodes if n["kind"] == "skill"),
            "runbooks":   sum(1 for n in nodes if n["kind"] == "runbook"),
            "collections": sum(1 for n in nodes if n["kind"] == "collection"),
            "edges":      len(edges),
        },
    }
