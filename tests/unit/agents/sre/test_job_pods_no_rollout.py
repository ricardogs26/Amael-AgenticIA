"""
Tests: los pods de Job no se remedian con ROLLOUT_RESTART.

Incidente 03-ago-2026: los pods de `amael-watchdog` (CronJob) fallan por diseño
—exit 1 cuando detectan un deployment caído, eso ES la alerta— y Raphael los
trataba como pods de Deployment. Como owner_name quedaba vacío, el healer usaba
el nombre del POD y parcheaba `deployments/amael-watchdog-29762490-88lpk`, que
no existe: 16 patches 404 en 6 horas, uno por ciclo.
"""
from __future__ import annotations

from types import SimpleNamespace

from agents.sre.models import Anomaly


def _job_anomaly(issue_type="POD_FAILED") -> Anomaly:
    return Anomaly(
        issue_type=issue_type,
        severity="HIGH",
        namespace="amael-ia",
        resource_name="amael-watchdog-29762490-88lpk",
        resource_type="Pod",
        details="Job pod en Error",
        owner_name="amael-watchdog-29762490",
        metadata={"owner_kind": "Job"},
    )


def _deploy_anomaly(issue_type="POD_FAILED") -> Anomaly:
    return Anomaly(
        issue_type=issue_type,
        severity="HIGH",
        namespace="amael-ia",
        resource_name="trader-service-84c5fbf49-hnltc",
        resource_type="Pod",
        details="pod en Error",
        owner_name="trader-service",
        metadata={"owner_kind": "Deployment"},
    )


# ── Pods de Job ──────────────────────────────────────────────────────────────

def test_job_pod_never_rollout_restarts():
    from agents.sre.healer import decide_action
    assert decide_action(_job_anomaly(), confidence=0.99) == "NO_ACTION"


def test_job_pod_blocked_for_every_auto_healable_type():
    """El bucle 404 se veía con POD_FAILED, pero aplica a todos."""
    from agents.sre.healer import decide_action
    for t in ("POD_FAILED", "CRASH_LOOP", "OOM_KILLED", "HIGH_RESTARTS"):
        assert decide_action(_job_anomaly(t), confidence=0.99) == "NO_ACTION", t


def test_job_check_runs_before_k8s_calls(monkeypatch):
    """No debe consultar la API para decidir sobre un pod de Job."""
    from agents.sre import healer

    def _boom(*_a, **_kw):
        raise AssertionError("no debe consultar K8s para un pod de Job")

    monkeypatch.setattr(healer, "_get_k8s_apps", _boom)
    assert healer.decide_action(_job_anomaly(), confidence=0.99) == "NO_ACTION"


def test_deployment_pod_still_restarts(monkeypatch):
    """El caso normal no se ve afectado."""
    from agents.sre import healer

    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda *_a: True)
    assert healer.decide_action(_deploy_anomaly(), confidence=0.99) == "ROLLOUT_RESTART"


def test_anomaly_without_metadata_is_not_treated_as_job(monkeypatch):
    """Anomalías viejas sin metadata mantienen el comportamiento previo."""
    from agents.sre import healer

    a = _deploy_anomaly()
    a.metadata = {}
    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda *_a: True)
    assert healer.decide_action(a, confidence=0.99) == "ROLLOUT_RESTART"


# ── Verificación de existencia del Deployment ────────────────────────────────

def test_missing_deployment_notifies_instead_of_patching(monkeypatch):
    from agents.sre import healer

    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda *_a: False)
    assert healer.decide_action(_deploy_anomaly(), confidence=0.99) == "NOTIFY_HUMAN"


def test_deployment_exists_false_on_404(monkeypatch):
    from agents.sre import healer

    def _raise_404(**_kw):
        raise _mk_api_exc(404)

    monkeypatch.setattr(
        healer, "_get_k8s_apps",
        lambda: SimpleNamespace(read_namespaced_deployment=_raise_404),
    )
    assert healer._deployment_exists("no-existe", "amael-ia") is False


def test_deployment_exists_true_when_check_inconclusive(monkeypatch):
    """Un 403 de RBAC no prueba ausencia: no bloquear la remediación por eso."""
    from agents.sre import healer

    def _raise_403(**_kw):
        raise _mk_api_exc(403)

    monkeypatch.setattr(
        healer, "_get_k8s_apps",
        lambda: SimpleNamespace(read_namespaced_deployment=_raise_403),
    )
    assert healer._deployment_exists("algo", "amael-ia") is True


def _mk_api_exc(status: int) -> Exception:
    exc = Exception(f"({status}) Reason: test")
    exc.status = status
    return exc
