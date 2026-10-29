"""
Raphael vigila y repara el namespace `trader` (separado de amael-ia) pero Camael
no abre PRs por el trader: su manifest vive en otro repo.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.sre import healer, observer


def _anomalia(ns="trader"):
    return SimpleNamespace(
        issue_type="CRASH_LOOP", severity="HIGH", namespace=ns,
        resource_name="trader-service-84c5fbf49-hnltc", owner_name="trader-service",
        details="", restart_count=5,
    )


def test_observer_vigila_trader_por_defecto():
    assert "trader" in observer._OBSERVE_NAMESPACES


def test_reparacion_usa_el_namespace_del_incidente():
    """rollout_restart parchea en el namespace de la anomalía, no en amael-ia."""
    apps = MagicMock()
    with patch.object(healer, "_get_k8s_apps", return_value=apps):
        healer.rollout_restart("trader-service", "trader")
    assert apps.patch_namespaced_deployment.call_args.kwargs["namespace"] == "trader"


def test_unmapped_deployment_no_camael_escalation():
    """Sin entrada en APP_MANIFEST_MAP: solo aviso, sin handoff ni cliente de Camael."""
    avisos = []
    with patch("agents.sre.argocd_discovery.discover_manifest", return_value=None), \
         patch("clients._http.get_camael_client") as camael:
        healer.handoff_to_camael(_anomalia(), "inc-1", avisos.append)
    camael.assert_not_called()
    assert len(avisos) == 1
    assert "trader-service" in avisos[0]
