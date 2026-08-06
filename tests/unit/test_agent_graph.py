"""
Tests del grafo de agentes (`observability/agent_graph.py`).

Lo que se protege aquí es que la topología siga derivándose del código. El
diagrama anterior vivía escrito a mano en un panel de texto de Grafana y se
desactualizaba sin que nadie se enterara; si estos tests se rompen porque
alguien registró un agente, el grafo está haciendo exactamente su trabajo.
"""
from __future__ import annotations

import pytest

from agents.base.agent_registry import register_all_agents
from observability import agent_graph


@pytest.fixture(scope="module", autouse=True)
def _registry():
    register_all_agents()


@pytest.fixture
def grafo():
    return agent_graph.build_graph(include_traffic=False)


# ── Topología ─────────────────────────────────────────────────────────────────

def test_incluye_los_cuatro_nodos_del_pipeline(grafo):
    pipeline = {n["id"] for n in grafo["nodes"] if n["kind"] == "pipeline"}
    assert pipeline == {"planner", "grouper", "batch_executor", "supervisor"}


def test_entrada_y_salida_son_nodos_distintos(grafo):
    kinds = {n["id"]: n["kind"] for n in grafo["nodes"]}
    assert kinds["user"] == "entry"
    assert kinds["end"] == "exit"


def test_el_self_loop_del_executor_esta_presente(grafo):
    """El ciclo de batches es la única arista reflexiva del pipeline."""
    reflexivas = {(e["source"], e["target"]) for e in grafo["edges"]
                  if e["source"] == e["target"]}
    assert ("batch_executor", "batch_executor") in reflexivas


def test_el_replan_cierra_el_ciclo_hacia_el_planner(grafo):
    aristas = {(e["source"], e["target"]) for e in grafo["edges"]}
    assert ("supervisor", "planner") in aristas
    assert ("supervisor", "end") in aristas


def test_los_agentes_registrados_aparecen_como_nodos(grafo):
    from agents.base.agent_registry import AgentRegistry
    registrados = set(AgentRegistry.names())
    en_grafo    = {n["id"] for n in grafo["nodes"] if n["kind"] == "agent"}
    assert registrados == en_grafo


def test_todo_agente_de_dispatch_directo_cuelga_de_user(grafo):
    from orchestration.agent_dispatcher import _DIRECT_DISPATCH
    desde_user = {e["target"] for e in grafo["edges"]
                  if e["source"] == "user" and e["via"] == "dispatch"}
    assert set(_DIRECT_DISPATCH.values()) <= desde_user


def test_no_hay_nodos_ni_aristas_duplicados(grafo):
    ids = [n["id"] for n in grafo["nodes"]]
    assert len(ids) == len(set(ids))
    claves = [(e["source"], e["target"], e["via"]) for e in grafo["edges"]]
    assert len(claves) == len(set(claves))


def test_toda_arista_apunta_a_un_nodo_existente(grafo):
    """Una arista huérfana rompe el layout del front sin error visible."""
    ids = {n["id"] for n in grafo["nodes"]}
    huerfanas = [
        (e["source"], e["target"]) for e in grafo["edges"]
        if e["source"] not in ids or e["target"] not in ids
    ]
    assert not huerfanas


# ── Contrato de la métrica ────────────────────────────────────────────────────

def test_declara_que_la_metrica_es_acumulada(grafo):
    """
    El front necesita saber qué significa `invocations` para etiquetarlo bien.
    Que el número sea acumulado y no una tasa es la diferencia entre «se usó 8
    veces» y «se usa 8 veces por segundo».
    """
    assert grafo["metric"] == "cumulative_since_pod_start"


def test_no_hay_ventana_configurable(grafo):
    """
    La ventana se quitó junto con `rate`/`increase`. Además de simplificar,
    elimina la concatenación de un parámetro del usuario dentro de la PromQL —
    la única superficie de inyección que tenía este módulo.
    """
    assert "window" not in grafo
    import inspect
    assert "window" not in inspect.signature(agent_graph.build_graph).parameters


# ── Tráfico ───────────────────────────────────────────────────────────────────

def test_sin_prometheus_has_traffic_es_false(monkeypatch):
    """Prometheus caído debe verse distinto de un sistema ocioso."""
    monkeypatch.setattr(agent_graph, "_query_vector", lambda *a, **k: [])
    grafo = agent_graph.build_graph()
    assert grafo["has_traffic"] is False
    assert all("invocations" not in e for e in grafo["edges"])


def test_la_consulta_lee_el_contador_crudo(monkeypatch):
    """
    Ni `rate` ni `increase`. Ambas miden variación DENTRO de una ventana, y en
    este laboratorio el tráfico son ráfagas cortas separadas por horas:

      - `rate[5m]` dio 0 en doce sondeos tras cinco turnos reales (6-ago-2026).
      - `increase[1h]` dio 0 con 24 muestras en la ventana, porque cada
        despliegue crea un pod nuevo y la ráfaga ocurrió antes del primer
        scrape: el contador ya estaba en su valor final la primera vez que
        Prometheus lo vio. Ninguna ventana lo recupera.

    Este test existe para que nadie lo "mejore" de vuelta a rate/increase.
    """
    vistas: list[str] = []
    monkeypatch.setattr(
        agent_graph, "_query_vector", lambda q, timeout=5.0: vistas.append(q) or []
    )
    agent_graph.build_graph()
    assert vistas
    assert not any("rate(" in q or "increase(" in q for q in vistas)
    assert any("amael_agent_edge_total" in q for q in vistas)
    assert any("amael_agent_executions_total" in q for q in vistas)


def test_las_invocaciones_se_asignan_a_la_arista_correcta(monkeypatch):
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [
                # Prometheus entrega los contadores como float.
                {"metric": {"source": "user", "target": "planner", "via": "pipeline"},
                 "value": [0, "8.0000000001"]},
            ]
        return [
            {"metric": {"agent_name": "raphael", "success": "true"}, "value": [0, "8"]},
            {"metric": {"agent_name": "raphael", "success": "false"}, "value": [0, "2"]},
        ]

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo = agent_graph.build_graph()
    assert grafo["has_traffic"] is True

    por_clave = {
        (e["source"], e["target"], e["via"]): e["invocations"] for e in grafo["edges"]
    }
    assert por_clave[("user", "planner", "pipeline")] == 8
    # Las demás aristas existen en la topología pero no tuvieron tráfico: eso es
    # un dato (camino muerto), no una ausencia.
    assert por_clave[("planner", "grouper", "pipeline")] == 0

    raphael = next(n for n in grafo["nodes"] if n["id"] == "raphael")
    assert raphael["invocations"] == 10
    assert raphael["error_rate"] == 0.2


def test_error_rate_es_none_cuando_no_hubo_ejecuciones(monkeypatch):
    """0/0 no es 0% de error — es ausencia de dato, y se reporta como tal."""
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [{"metric": {"source": "user", "target": "raphael", "via": "dispatch"},
                     "value": [0, "1"]}]
        return [{"metric": {"agent_name": "raphael", "success": "true"}, "value": [0, "0"]}]

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo   = agent_graph.build_graph()
    raphael = next(n for n in grafo["nodes"] if n["id"] == "raphael")
    assert raphael["error_rate"] is None


# ── Aristas descubiertas ──────────────────────────────────────────────────────

def test_una_arista_medida_pero_no_declarada_aparece_en_el_grafo(monkeypatch):
    """
    Caso real: `chat.py` llama a Zaphkiel, pero esa llamada no sale de
    `_DIRECT_DISPATCH` ni del grafo LangGraph. La métrica existía y el nodo
    mostraba invocaciones, pero ninguna línea las explicaba.
    """
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [{"metric": {"source": "user", "target": "zaphkiel", "via": "memory"},
                     "value": [0, "6"]}]
        return [{"metric": {"agent_name": "zaphkiel", "success": "true"}, "value": [0, "6"]}]

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo = agent_graph.build_graph()

    arista = next(
        (e for e in grafo["edges"]
         if (e["source"], e["target"], e["via"]) == ("user", "zaphkiel", "memory")),
        None,
    )
    assert arista is not None, "la arista medida no llegó al grafo"
    assert arista["invocations"] == 6
    assert arista["discovered"] is True


def test_las_aristas_declaradas_no_se_marcan_como_descubiertas(monkeypatch):
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [{"metric": {"source": "planner", "target": "grouper", "via": "pipeline"},
                     "value": [0, "3"]}]
        return []

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo = agent_graph.build_graph()
    arista = next(e for e in grafo["edges"]
                  if (e["source"], e["target"]) == ("planner", "grouper"))
    assert "discovered" not in arista
    assert arista["invocations"] == 3


def test_un_nodo_desconocido_medido_se_agrega_al_grafo(monkeypatch):
    """Un agente que aún no se registra pero ya emite métricas no debe perderse."""
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [{"metric": {"source": "raphael", "target": "agente_nuevo", "via": "handoff"},
                     "value": [0, "2"]}]
        return []

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo = agent_graph.build_graph()
    nodo = next((n for n in grafo["nodes"] if n["id"] == "agente_nuevo"), None)
    assert nodo is not None
    assert nodo["kind"] == "unknown"


def test_una_arista_medida_en_cero_no_ensucia_el_grafo(monkeypatch):
    """Series viejas de pods muertos siguen en Prometheus; no son conexiones vivas."""
    def fake(promql, timeout=5.0):
        if "amael_agent_edge_total" in promql:
            return [{"metric": {"source": "fantasma", "target": "otro", "via": "dispatch"},
                     "value": [0, "0"]}]
        return []

    monkeypatch.setattr(agent_graph, "_query_vector", fake)
    grafo = agent_graph.build_graph()
    assert not any(n["id"] == "fantasma" for n in grafo["nodes"])
