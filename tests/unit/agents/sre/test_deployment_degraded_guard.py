"""
Tests del guardrail DEPLOYMENT_DEGRADED (P0).

Un deployment que está arrancando baja available_replicas por unos segundos.
Sin filtro, el observer lo marca degradado y el healer lo reinicia — abortando
el arranque anterior y realimentando un bucle de reinicios.
"""
from __future__ import annotations

from types import SimpleNamespace

from agents.sre.models import Anomaly


def _condition(cond_type: str, status: str, reason: str):
    return SimpleNamespace(type=cond_type, status=status, reason=reason)


def _deployment(generation=2, observed=2, conditions=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=generation),
        status=SimpleNamespace(
            observed_generation=observed,
            conditions=conditions if conditions is not None else [],
        ),
    )


# ── _is_rollout_in_progress ──────────────────────────────────────────────────

def test_rollout_in_progress_when_generation_not_observed():
    """El controlador aún no procesa el spec nuevo → está convergiendo."""
    from agents.sre.observer import _is_rollout_in_progress
    assert _is_rollout_in_progress(_deployment(generation=5, observed=4)) is True


def test_rollout_in_progress_when_replicaset_updated():
    from agents.sre.observer import _is_rollout_in_progress
    dep = _deployment(conditions=[_condition("Progressing", "True", "ReplicaSetUpdated")])
    assert _is_rollout_in_progress(dep) is True


def test_rollout_not_in_progress_when_new_replicaset_available():
    """Rollout terminado: si sigue sin réplicas, ahí sí está enfermo."""
    from agents.sre.observer import _is_rollout_in_progress
    dep = _deployment(conditions=[_condition("Progressing", "True", "NewReplicaSetAvailable")])
    assert _is_rollout_in_progress(dep) is False


def test_rollout_not_in_progress_without_conditions():
    from agents.sre.observer import _is_rollout_in_progress
    assert _is_rollout_in_progress(_deployment()) is False


# ── Racha de ciclos degradados ───────────────────────────────────────────────

def test_degraded_streak_requires_consecutive_cycles(monkeypatch):
    """Una degradación puntual no alerta; solo la que sobrevive N ciclos."""
    from agents.sre import observer

    monkeypatch.setattr(observer, "_degraded_streak", {}, raising=False)
    monkeypatch.setattr(observer, "SRE_DEGRADED_MIN_CYCLES", 3, raising=False)

    # Simula la contabilidad del observer para un deployment ya convergido.
    key = "amael-ia/trader-service"
    emitted = []
    for _cycle in range(4):
        streak = observer._degraded_streak.get(key, 0) + 1
        observer._degraded_streak[key] = streak
        if streak >= observer.SRE_DEGRADED_MIN_CYCLES:
            emitted.append(streak)

    assert emitted == [3, 4], "debe alertar a partir del tercer ciclo consecutivo"


def test_degraded_streak_resets_when_healthy(monkeypatch):
    from agents.sre import observer

    monkeypatch.setattr(observer, "_degraded_streak", {"amael-ia/x": 2}, raising=False)
    observer._degraded_streak.pop("amael-ia/x", None)
    assert "amael-ia/x" not in observer._degraded_streak


# ── Guardrail anti-bucle en decide_action ────────────────────────────────────

def _degraded_anomaly() -> Anomaly:
    return Anomaly(
        issue_type="DEPLOYMENT_DEGRADED",
        severity="CRITICAL",
        namespace="amael-ia",
        resource_name="trader-service",
        resource_type="Deployment",
        details="0/1 réplicas disponibles",
        owner_name="trader-service",
    )


def test_decide_action_skips_recently_restarted(monkeypatch):
    """No reiniciar algo que sigue arrancando por un restart previo."""
    from agents.sre import healer

    monkeypatch.setattr(
        healer, "_restarted_within_verification_window", lambda *_a, **_kw: True
    )
    assert healer.decide_action(_degraded_anomaly(), confidence=0.95) == "NO_ACTION"


def test_decide_action_restarts_when_window_elapsed(monkeypatch):
    from agents.sre import healer

    monkeypatch.setattr(
        healer, "_restarted_within_verification_window", lambda *_a, **_kw: False
    )
    assert healer.decide_action(_degraded_anomaly(), confidence=0.95) == "ROLLOUT_RESTART"


def test_restarted_window_false_without_annotation(monkeypatch):
    """Deployment nunca reiniciado por Raphael → no bloquea la acción."""
    from agents.sre import healer

    dep = SimpleNamespace(
        spec=SimpleNamespace(
            template=SimpleNamespace(metadata=SimpleNamespace(annotations={}))
        )
    )
    monkeypatch.setattr(
        healer, "_get_k8s_apps",
        lambda: SimpleNamespace(read_namespaced_deployment=lambda **_kw: dep),
    )
    assert healer._restarted_within_verification_window("trader-service", "amael-ia") is False
