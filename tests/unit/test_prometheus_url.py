"""
Regresión: la URL de Prometheus debe salir del settings, no del fallback.

`observability/slo.py` leía `settings.prometheus_url`; el atributo se llama
`sre_prometheus_url`. Como el acceso era un `getattr` con default, nunca falló
en ruidoso: simplemente usaba siempre `prometheus-server.observability:80`, que
no resuelve en el cluster. Toda query de SLO devolvía None y se leía como
«sin datos» en vez de como error de configuración.

Los tests no importan `config.settings`: instanciarlo exige el entorno completo
del pod. Se stubea el módulo, que es justo lo que ejercita el nombre del
atributo — el punto de la regresión.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

from observability.slo import _FALLBACK_PROM, _prometheus_url

_URL_PROBADA = "http://prometheus-de-prueba:9090"


def _stub_settings(monkeypatch, **attrs):
    modulo = types.ModuleType("config.settings")
    modulo.settings = types.SimpleNamespace(**attrs)
    monkeypatch.setitem(sys.modules, "config.settings", modulo)


def test_el_campo_del_settings_se_llama_sre_prometheus_url():
    """Si alguien lo renombra, esto truena aquí y no en producción."""
    fuente = (Path(__file__).resolve().parents[2] / "config" / "settings.py").read_text()
    campos = {
        node.target.id
        for node in ast.walk(ast.parse(fuente))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "sre_prometheus_url" in campos


def test_usa_el_valor_del_settings(monkeypatch):
    _stub_settings(monkeypatch, sre_prometheus_url=_URL_PROBADA)
    assert _prometheus_url() == _URL_PROBADA


def test_ignora_un_atributo_con_el_nombre_viejo(monkeypatch):
    """Justo el bug: `prometheus_url` no es el campo, y leerlo daba el fallback."""
    _stub_settings(monkeypatch, prometheus_url="http://no-usar:80")
    assert _prometheus_url() == _FALLBACK_PROM


def test_el_fallback_no_apunta_al_servicio_inexistente():
    """`prometheus-server.observability` no existe en este cluster."""
    assert "prometheus-server.observability" not in _FALLBACK_PROM
    assert "kube-prometheus-stack-prometheus" in _FALLBACK_PROM


def test_el_grafo_comparte_la_misma_url():
    """
    `agent_graph._query_vector` importa este helper: si divergen, el grafo y los
    SLOs consultarían Prometheus distintos.
    """
    import inspect

    from observability import agent_graph
    assert "_prometheus_url" in inspect.getsource(agent_graph._query_vector)


# ── NaN / Inf ─────────────────────────────────────────────────────────────────
# `histogram_quantile` sobre un handler sin tráfico devuelve NaN, y una división
# por cero devuelve +Inf. Ninguno es JSON válido: llegaban intactos a la
# respuesta y `/api/slo/status` daba 500 «Out of range float values are not JSON
# compliant». El bug estuvo tapado mientras Prometheus era inalcanzable, porque
# entonces todo era None y nunca se evaluaba un float real.

class _RespFalsa:
    def __init__(self, valor):
        self._valor = valor

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"result": [{"metric": {}, "value": [0, self._valor]}]}}


@pytest.mark.parametrize("valor", ["NaN", "+Inf", "-Inf"])
def test_query_descarta_valores_no_finitos(monkeypatch, valor):
    import httpx

    from observability.slo import _query
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _RespFalsa(valor))
    assert _query("cualquier_cosa") is None


def test_query_conserva_los_valores_normales(monkeypatch):
    import httpx

    from observability.slo import _query
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _RespFalsa("0.997"))
    assert _query("cualquier_cosa") == 0.997


@pytest.mark.parametrize("valor,esperado", [("NaN", 0.0), ("+Inf", 0.0), ("3.5", 3.5)])
def test_el_grafo_normaliza_no_finitos_a_cero(valor, esperado):
    from observability.agent_graph import _valor as leer
    assert leer({"metric": {}, "value": [0, valor]}) == esperado


def test_el_grafo_tolera_series_malformadas():
    from observability.agent_graph import _valor as leer
    assert leer({"metric": {}}) == 0.0
    assert leer({"metric": {}, "value": [0, "no-es-numero"]}) == 0.0
