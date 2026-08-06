"""
Grafo de agentes — topología derivada del código + tráfico real de Prometheus.

El diagrama del dashboard #11 estaba escrito a mano en un panel de texto: se
desactualizaba en silencio cada vez que alguien registraba un agente. Aquí la
topología se deriva de las tres fuentes de verdad que ya existen, así que un
agente nuevo aparece en el grafo sin que nadie edite un diagrama:

  1. El grafo LangGraph compilado  → nodos y aristas del pipeline
  2. `AgentRegistry`               → agentes de dispatch directo y su metadata
  3. `required_skills` de cada uno → aristas agente → skill

Encima de esa topología se pinta el tráfico medido (`amael_agent_edge_total`).
Una arista declarada pero con rate 0 es información útil: significa camino
muerto, no error de dibujo.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("observability.agent_graph")

# LangGraph nombra así a sus nodos sentinela. En el grafo los mostramos con los
# nombres que usa la instrumentación de `workflow_engine.py`.
_SENTINELS      = {"__start__": "user", "__end__": "end"}
_SENTINEL_KINDS = {"__start__": "entry", "__end__": "exit"}

# Las consultas son contadores crudos, sin ventana. Ver `_apply_traffic` para el
# porqué; el corolario grato es que ya no se concatena nada en la PromQL, así que
# desaparece la superficie de inyección que obligaba a una lista blanca.


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

def build_graph(include_traffic: bool = True) -> dict:
    """Grafo completo listo para renderizar: `{nodes, edges, metric, has_traffic}`."""
    pipeline_nodes, pipeline_edges = _pipeline_topology()
    agent_nodes,    agent_edges    = _agent_topology()

    nodes = _dedupe_nodes(pipeline_nodes + agent_nodes)
    edges = _dedupe_edges(pipeline_edges + agent_edges)

    has_traffic = _apply_traffic(nodes, edges) if include_traffic else False
    return {
        "nodes":       nodes,
        "edges":       edges,
        # Contrato explícito para el front: `invocations` es acumulado desde el
        # arranque del pod, no un promedio ni una ventana.
        "metric":      "cumulative_since_pod_start",
        "has_traffic": has_traffic,
        "counts": {
            "agents":   sum(1 for n in nodes if n["kind"] == "agent"),
            "pipeline": sum(1 for n in nodes if n["kind"] == "pipeline"),
            "skills":   sum(1 for n in nodes if n["kind"] == "skill"),
            "edges":    len(edges),
        },
    }
