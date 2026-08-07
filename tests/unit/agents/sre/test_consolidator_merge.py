"""
Tests del fix C0 del consolidador de runbooks (fusión + drenaje).

El defecto que se fija: cada corrida nocturna borraba el consolidado anterior
y sintetizaba SOLO el lote nuevo — el runbook de cada tipo olvidaba cada 24 h
lo aprendido en todas las noches anteriores. Con el backlog medido el
7-ago-2026 (626 HIGH_RESTARTS a 10/noche), tras dos meses de drenaje el
documento final solo habría sabido de los últimos 10 incidentes.
"""
from __future__ import annotations

import pytest

from agents.sre import runbook_consolidator as rc


@pytest.fixture
def entorno(monkeypatch):
    """Aísla run_consolidation: 40 crudos de un tipo + un consolidado previo."""
    crudos = [
        {"id": i, "text": f"incidente {i}", "resource_name": "ollama",
         "namespace": "amael-ia", "action_taken": "ROLLOUT_RESTART",
         "timestamp": f"2026-06-{(i % 28) + 1:02d}"}
        for i in range(40)
    ]
    llamadas: dict = {"synth": None, "saved": None, "deleted_ids": None,
                      "prev_deleted": 0}

    monkeypatch.setattr(rc, "_fetch_auto_runbooks_by_type",
                        lambda: {"HIGH_RESTARTS": crudos})
    monkeypatch.setattr(
        rc, "_fetch_previous_consolidated",
        lambda t: {"text": "## Conocimiento acumulado previo",
                   "consolidated_from": 60},
    )

    def synth(issue_type, lote, previous_text=""):
        llamadas["synth"] = {"lote": len(lote), "previous": previous_text}
        return "# Runbook consolidado fusionado"

    def save(issue_type, text, source_count):
        llamadas["saved"] = {"text": text, "source_count": source_count}
        return True

    def delete_prev(issue_type):
        llamadas["prev_deleted"] += 1
        return 1

    monkeypatch.setattr(rc, "_synthesize_runbooks", synth)
    monkeypatch.setattr(rc, "_save_consolidated_runbook", save)
    monkeypatch.setattr(rc, "_delete_previous_consolidated", delete_prev)
    monkeypatch.setattr(rc, "_delete_runbook_points",
                        lambda ids: llamadas.__setitem__("deleted_ids", ids))
    return llamadas


def test_el_consolidado_previo_entra_a_la_sintesis(entorno):
    rc.run_consolidation()
    assert entorno["synth"]["previous"] == "## Conocimiento acumulado previo", \
        "la síntesis no recibió el conocimiento previo — cada noche se olvidaba todo"


def test_el_lote_respeta_el_tope_y_solo_ese_lote_se_borra(entorno, monkeypatch):
    monkeypatch.setattr(rc, "MAX_RUNBOOKS_PER_TYPE", 30)
    rc.run_consolidation()
    assert entorno["synth"]["lote"] == 30
    assert len(entorno["deleted_ids"]) == 30, \
        "borrar más crudos de los sintetizados pierde incidentes sin destilar"


def test_source_count_acumula_lo_previo(entorno, monkeypatch):
    monkeypatch.setattr(rc, "MAX_RUNBOOKS_PER_TYPE", 30)
    rc.run_consolidation()
    # 30 del lote + 60 que el consolidado previo ya representaba
    assert entorno["saved"]["source_count"] == 90


def test_sin_previo_la_sintesis_recibe_vacio(entorno, monkeypatch):
    monkeypatch.setattr(rc, "_fetch_previous_consolidated", lambda t: None)
    rc.run_consolidation()
    assert entorno["synth"]["previous"] == ""
    assert entorno["saved"]["source_count"] == rc.MAX_RUNBOOKS_PER_TYPE


def test_si_la_sintesis_falla_el_previo_no_se_borra(entorno, monkeypatch):
    monkeypatch.setattr(rc, "_synthesize_runbooks", lambda *a, **k: "")
    rc.run_consolidation()
    assert entorno["prev_deleted"] == 0, \
        "borró el consolidado vigente sin tener reemplazo"
    assert entorno["deleted_ids"] is None


def test_el_tope_es_configurable_por_env(monkeypatch):
    """La velocidad de drenaje del backlog es config, no una constante."""
    import importlib
    monkeypatch.setenv("SRE_CONSOLIDATOR_MAX_PER_TYPE", "50")
    importlib.reload(rc)
    try:
        assert rc.MAX_RUNBOOKS_PER_TYPE == 50
    finally:
        monkeypatch.delenv("SRE_CONSOLIDATOR_MAX_PER_TYPE")
        importlib.reload(rc)
