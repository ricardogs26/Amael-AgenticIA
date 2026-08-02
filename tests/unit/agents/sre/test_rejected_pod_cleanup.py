"""Tests de POD_REJECTED → DELETE_STUCK_POD.

Caso real (02-ago-2026): tras reiniciar el nodo, el device plugin de NVIDIA aún
reportaba la GPU como no saludable y el kubelet rechazó un pod de ollama con
`UnexpectedAdmissionError`. El ReplicaSet creó el reemplazo, que corrió sin
problema, pero la lápida quedó en fase Failed y Raphael la reportaba como
POD_FAILED en cada ciclo — alerta por WhatsApp indefinida sin remediación
posible (un pod Failed no se reinicia).
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from agents.sre.models import Anomaly


def _rejected_anomaly(
    resource: str = "ollama-deployment-795dcc85d9-nz52f",
    owner: str = "ollama-deployment",
    has_owner: bool = True,
    reason: str = "UnexpectedAdmissionError",
) -> Anomaly:
    return Anomaly(
        issue_type="POD_REJECTED",
        severity="LOW" if has_owner else "HIGH",
        namespace="amael-ia",
        resource_name=resource,
        resource_type="Pod",
        details=f"rechazado por el kubelet ({reason})",
        owner_name=owner,
        metadata={"reject_reason": reason, "has_owner": has_owner},
    )


# --- decide_action ---------------------------------------------------------

def test_decide_action_deletes_when_controller_will_reschedule():
    from agents.sre.healer import decide_action
    assert decide_action(_rejected_anomaly(), confidence=1.0) == "DELETE_STUCK_POD"


def test_decide_action_deletes_even_for_protected_deployment():
    """ollama-deployment está protegido, pero la lápida ya no ejecuta nada."""
    from agents.sre.healer import decide_action
    assert decide_action(_rejected_anomaly(), confidence=1.0) == "DELETE_STUCK_POD"


def test_decide_action_notifies_when_pod_has_no_owner():
    """Sin controlador nadie recrea el pod: borrarlo lo pierde → avisar al humano."""
    from agents.sre.healer import decide_action
    anomaly = _rejected_anomaly(resource="debug-pod", owner="", has_owner=False)
    assert decide_action(anomaly, confidence=1.0) == "NOTIFY_HUMAN"


def test_decide_action_notifies_when_metadata_missing():
    """Sin evidencia de controlador, el default conservador es no borrar."""
    from agents.sre.healer import decide_action
    anomaly = _rejected_anomaly()
    anomaly.metadata = {}
    assert decide_action(anomaly, confidence=1.0) == "NOTIFY_HUMAN"


# --- observer --------------------------------------------------------------

def _observe_with_pod(pod):
    from agents.sre import observer

    class FakeCoreV1:
        def list_namespaced_pod(self, namespace):
            return NS(items=[pod])

        def list_node(self):
            return NS(items=[])

    class FakeK8s:
        def CoreV1Api(self):
            return FakeCoreV1()

        def AppsV1Api(self):
            return NS()

    orig = observer._get_k8s_client
    observer._get_k8s_client = lambda: FakeK8s()
    try:
        return observer.observe_cluster(namespaces=["amael-ia"])
    finally:
        observer._get_k8s_client = orig


def _rejected_pod(reason: str, owner_refs: list | None = None):
    """Pod rechazado en admisión: sin container_statuses, nunca corrió nada."""
    return NS(
        metadata=NS(
            name="ollama-deployment-795dcc85d9-nz52f",
            owner_references=owner_refs if owner_refs is not None else [
                NS(kind="ReplicaSet", name="ollama-deployment-795dcc85d9")
            ],
            creation_timestamp=None,
        ),
        status=NS(
            phase="Failed",
            reason=reason,
            message="Pod was rejected: Allocate failed due to no healthy devices present",
            container_statuses=None,
        ),
    )


@pytest.mark.parametrize("reason", [
    "UnexpectedAdmissionError",
    "Evicted",
    "NodeAffinity",
    "Shutdown",
    "OutOfnvidia.com/gpu",
])
def test_observer_detects_reject_reasons(reason):
    anomalies = _observe_with_pod(_rejected_pod(reason))
    types = [a.issue_type for a in anomalies]
    assert "POD_REJECTED" in types
    assert "POD_FAILED" not in types


def test_observer_records_owner_and_reason_in_metadata():
    anomalies = _observe_with_pod(_rejected_pod("UnexpectedAdmissionError"))
    rejected = next(a for a in anomalies if a.issue_type == "POD_REJECTED")
    assert rejected.metadata["has_owner"] is True
    assert rejected.metadata["reject_reason"] == "UnexpectedAdmissionError"
    assert rejected.owner_name == "ollama-deployment"


def test_observer_marks_ownerless_pod():
    anomalies = _observe_with_pod(_rejected_pod("Evicted", owner_refs=[]))
    rejected = next(a for a in anomalies if a.issue_type == "POD_REJECTED")
    assert rejected.metadata["has_owner"] is False


def test_observer_keeps_pod_failed_for_app_failures():
    """Un Failed sin razón de rechazo sigue siendo POD_FAILED (fallo real de la app)."""
    anomalies = _observe_with_pod(_rejected_pod(reason="Error"))
    types = [a.issue_type for a in anomalies]
    assert "POD_FAILED" in types
    assert "POD_REJECTED" not in types


# --- correlación -----------------------------------------------------------

def test_correlation_preserves_has_owner():
    """≥3 pods rechazados se agrupan; el grupo debe seguir siendo borrable."""
    from agents.sre.detector import correlate_anomalies
    from agents.sre.healer import decide_action

    group = [
        _rejected_anomaly(resource=f"ollama-deployment-795dcc85d9-{i}")
        for i in range(3)
    ]
    correlated = correlate_anomalies(group)

    assert len(correlated) == 1
    grouped = correlated[0]
    assert grouped.resource_type == "Deployment"
    assert grouped.metadata["has_owner"] is True
    assert len(grouped.metadata["pod_names"]) == 3
    assert decide_action(grouped, confidence=1.0) == "DELETE_STUCK_POD"


# --- ejecución -------------------------------------------------------------

def test_execute_sre_action_deletes_rejected_pod(monkeypatch):
    from agents.sre import healer

    calls = []
    monkeypatch.setattr(
        healer, "delete_stuck_pod",
        lambda pod, ns: calls.append((pod, ns)) or "✅ DELETE_STUCK_POD ok",
    )
    monkeypatch.setattr("agents.sre.autonomy.get_agent_mode", lambda: "standard")

    result = healer.execute_sre_action(_rejected_anomaly(), "DELETE_STUCK_POD", None)
    assert calls == [("ollama-deployment-795dcc85d9-nz52f", "amael-ia")]
    assert "✅" in result
