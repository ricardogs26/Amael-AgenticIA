"""
Regresión del incidente ollama (31-jul-2026).

Tras el reinicio del nodo quedaron 5 pods de ollama-deployment en
ContainerStatusUnknown. El detector los correlacionó en una sola anomalía con
`resource_name = "ollama-deployment"` (el Deployment) y el healer intentó
`delete_namespaced_pod("ollama-deployment")` → 404 en bucle durante ~15 h.
"""
from unittest.mock import patch

from agents.sre.detector import correlate_anomalies
from agents.sre.healer import execute_sre_action
from agents.sre.models import Anomaly
from core.constants import ActionType, AnomalyType, Severity


def _stuck_pod(name: str) -> Anomaly:
    return Anomaly(
        issue_type=AnomalyType.POD_STATUS_UNKNOWN,
        severity=Severity.HIGH,
        namespace="amael-ia",
        resource_name=name,
        resource_type="Pod",
        owner_name="ollama-deployment",
        details="ContainerStatusUnknown tras reinicio del nodo",
    )


def test_correlacion_conserva_los_nombres_reales_de_pods():
    pods = [_stuck_pod(f"ollama-deployment-795dcc85d9-p{i}") for i in range(5)]

    result = correlate_anomalies(pods)

    correlated = [a for a in result if a.resource_type == "Deployment"]
    assert len(correlated) == 1
    assert correlated[0].resource_name == "ollama-deployment"
    assert correlated[0].metadata["pod_names"] == [p.resource_name for p in pods]


def test_delete_stuck_pod_borra_los_pods_no_el_deployment():
    pods = [_stuck_pod(f"ollama-deployment-795dcc85d9-p{i}") for i in range(5)]
    correlated = next(
        a for a in correlate_anomalies(pods) if a.resource_type == "Deployment"
    )

    with patch("agents.sre.healer.delete_stuck_pod") as mock_delete:
        mock_delete.return_value = "✅ DELETE_STUCK_POD: pod huérfano eliminado."
        result = execute_sre_action(correlated, ActionType.DELETE_STUCK_POD, lambda *a: None)

    borrados = [call.args[0] for call in mock_delete.call_args_list]
    assert borrados == [p.resource_name for p in pods]
    # El bug original: se llamaba con el nombre del Deployment.
    assert "ollama-deployment" not in borrados
    assert "✅" in result


def test_deployment_sin_pod_names_no_intenta_borrar_el_deployment():
    """Anomalía de Deployment sin metadata → se omite en vez de dar 404."""
    huerfana = Anomaly(
        issue_type=AnomalyType.POD_STATUS_UNKNOWN,
        severity=Severity.HIGH,
        namespace="amael-ia",
        resource_name="ollama-deployment",
        resource_type="Deployment",
        owner_name="ollama-deployment",
        details="sin metadata",
    )

    with patch("agents.sre.healer.delete_stuck_pod") as mock_delete:
        result = execute_sre_action(huerfana, ActionType.DELETE_STUCK_POD, lambda *a: None)

    mock_delete.assert_not_called()
    assert "se omite" in result
