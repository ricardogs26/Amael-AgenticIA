"""Tests de POD_STATUS_UNKNOWN → DELETE_STUCK_POD (reemplazo del CronJob
legacy clean-unknown-pods)."""
from __future__ import annotations

import pytest

from agents.sre.models import Anomaly


def _unknown_anomaly(resource: str = "demo-pod-abc123", owner: str = "demo-pod") -> Anomaly:
    return Anomaly(
        issue_type="POD_STATUS_UNKNOWN",
        severity="LOW",
        namespace="observability",
        resource_name=resource,
        resource_type="Pod",
        details="huérfano tras reinicio",
        owner_name=owner,
    )


def test_decide_action_returns_delete_stuck_pod():
    from agents.sre.healer import decide_action
    assert decide_action(_unknown_anomaly(), confidence=1.0) == "DELETE_STUCK_POD"


def test_decide_action_delete_even_for_protected_deployment():
    """Borrar el cadáver de un deployment protegido es seguro (contenedor ya no existe)."""
    from agents.sre.healer import decide_action
    anomaly = _unknown_anomaly(resource="ollama-deployment-x-y", owner="ollama-deployment")
    assert decide_action(anomaly, confidence=1.0) == "DELETE_STUCK_POD"


@pytest.mark.parametrize("mode", ["conservative", "standard", "full"])
def test_mode_allows_delete_stuck_pod(monkeypatch, mode):
    from agents.sre import autonomy
    monkeypatch.setattr(autonomy, "get_agent_mode", lambda: mode)
    assert autonomy.apply_mode_to_action("DELETE_STUCK_POD", 1.0) == "DELETE_STUCK_POD"


def test_mode_observe_blocks_delete_stuck_pod(monkeypatch):
    from agents.sre import autonomy
    monkeypatch.setattr(autonomy, "get_agent_mode", lambda: "observe")
    assert autonomy.apply_mode_to_action("DELETE_STUCK_POD", 1.0) == "OBSERVE_ONLY"


def test_execute_sre_action_deletes_pod(monkeypatch):
    from agents.sre import healer

    calls = []
    monkeypatch.setattr(
        healer, "delete_stuck_pod",
        lambda pod, ns: calls.append((pod, ns)) or "✅ DELETE_STUCK_POD ok",
    )
    monkeypatch.setattr(
        "agents.sre.autonomy.get_agent_mode", lambda: "standard"
    )
    result = healer.execute_sre_action(_unknown_anomaly(), "DELETE_STUCK_POD", None)
    assert calls == [("demo-pod-abc123", "observability")]
    assert "✅" in result


def test_observer_detects_container_status_unknown():
    """La detección marca POD_STATUS_UNKNOWN (no POD_FAILED) para cadáveres."""
    from types import SimpleNamespace as NS

    from agents.sre import observer

    terminated = NS(reason="ContainerStatusUnknown", exit_code=137)
    cs  = NS(restart_count=0, state=NS(waiting=None, terminated=terminated, running=None),
             last_state=NS(terminated=None))
    pod = NS(
        metadata=NS(name="dead-pod-123", owner_references=[], creation_timestamp=None),
        status=NS(phase="Failed", container_statuses=[cs]),
    )

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
        anomalies = observer.observe_cluster(namespaces=["amael-ia"])
    finally:
        observer._get_k8s_client = orig

    types = [a.issue_type for a in anomalies]
    assert "POD_STATUS_UNKNOWN" in types
    assert "POD_FAILED" not in types
