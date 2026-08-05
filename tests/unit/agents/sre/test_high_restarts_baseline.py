"""Tests del baseline de HIGH_RESTARTS en pods multi-contenedor.

Caso real (05-ago-2026): `kube-prometheus-stack-grafana` (3 contenedores con
299/303/307 reinicios) y `prometheus-kube-prometheus-stack-prometheus-0`
(2 contenedores con 298/296) llevaban meses corriendo sanos y Ready, pero
Raphael alertaba HIGH_RESTARTS por WhatsApp cada hora (TTL de dedup).

La causa: `observe_cluster()` itera `container_statuses` y cada contenedor
lleva su propio `restart_count`, pero el baseline se guardaba con una clave
por POD. Los contenedores se pisaban la entrada en cada ciclo, así que
siempre había alguno cuyo conteo "crecía" respecto al que escribió el
anterior — falso positivo perpetuo.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest


def _cs(name: str, restarts: int):
    """container_status sin estado de error: solo acumula reinicios."""
    return NS(name=name, restart_count=restarts, state=None, last_state=None)


def _pod(name: str, containers: list, ns: str = "observability"):
    return NS(
        metadata=NS(name=name, owner_references=[]),
        status=NS(phase="Running", container_statuses=containers,
                  conditions=[], start_time=None, reason=None, message=None),
    )


@pytest.fixture
def observer(monkeypatch):
    """observe_cluster con el cliente k8s y el baseline aislados."""
    from agents.sre import observer as obs

    obs._restart_baseline.clear()

    pods_holder = {"items": []}

    class _V1:
        def list_namespaced_pod(self, namespace):
            return NS(items=pods_holder["items"])

    monkeypatch.setattr(
        obs, "_get_k8s_client",
        lambda: NS(CoreV1Api=lambda: _V1(), AppsV1Api=lambda: NS()),
    )
    # Sin nodos: este test solo mira pods.
    monkeypatch.setattr(obs, "_OBSERVE_NAMESPACES", ["observability"])

    def _run(pods):
        pods_holder["items"] = pods
        found = obs.observe_cluster(namespaces=["observability"])
        return [a for a in found if a.issue_type == "HIGH_RESTARTS"]

    return _run


# --- el bug ----------------------------------------------------------------

def test_multi_container_pod_estable_no_alerta(observer):
    """Grafana: 3 contenedores, conteos distintos, ninguno crece → sin alerta."""
    grafana = _pod("kube-prometheus-stack-grafana-bf67656f-czls7", [
        _cs("grafana", 299),
        _cs("grafana-sc-dashboard", 303),
        _cs("grafana-sc-datasources", 307),
    ])

    assert observer([grafana]) == [], "primer ciclo: solo establece baseline"

    # Varios ciclos con el pod completamente estable.
    for _ in range(5):
        assert observer([grafana]) == [], "pod sano no debe alertar nunca"


def test_prometheus_dos_contenedores_estable_no_alerta(observer):
    """El conteo del 2º contenedor es MENOR que el del 1º — alternaba cada ciclo."""
    prom = _pod("prometheus-kube-prometheus-stack-prometheus-0", [
        _cs("config-reloader", 298),
        _cs("prometheus", 296),
    ])

    observer([prom])
    for _ in range(4):
        assert observer([prom]) == []


# --- la detección real sigue viva ------------------------------------------

def test_reinicio_real_de_un_contenedor_si_alerta(observer):
    """Cuando un contenedor concreto crece, se alerta solo por ese."""
    before = _pod("kube-prometheus-stack-grafana-bf67656f-czls7", [
        _cs("grafana", 299),
        _cs("grafana-sc-dashboard", 303),
    ])
    observer([before])  # baseline

    after = _pod("kube-prometheus-stack-grafana-bf67656f-czls7", [
        _cs("grafana", 301),           # +2 reinicios reales
        _cs("grafana-sc-dashboard", 303),
    ])
    found = observer([after])

    assert len(found) == 1
    assert "2 reinicio(s) nuevo(s)" in found[0].details
    assert "301" in found[0].details


def test_baseline_es_por_contenedor(observer):
    """La clave del baseline incluye el contenedor, no solo el pod."""
    from agents.sre import observer as obs

    observer([_pod("p", [_cs("a", 10), _cs("b", 20)], ns="observability")])

    assert "observability/p/a" in obs._restart_baseline
    assert "observability/p/b" in obs._restart_baseline
    assert obs._restart_baseline["observability/p/a"] == 10
    assert obs._restart_baseline["observability/p/b"] == 20
