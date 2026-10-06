"""
Tests: las anomalías de métricas (HIGH_MEMORY/HIGH_CPU) se pueden remediar.

Incidente noche 5→6-oct-2026: `whatsapp-personal` (Chromium) se quedó estable en
~85 % de su límite. `observe_metrics()` armaba la anomalía SIN owner_name, el
healer buscaba un Deployment llamado como el POD, recibía 404 y caía a
NOTIFY_HUMAN. Con el dedup de 15 min eso fue ~45 WhatsApps en una noche y el
pod nunca se reinició.

Mismo día 5-oct: frontend-next con owner_name correcto
(`frontend-next-deployment`) se renombraba a `frontend-next` por la
normalización de APP_MANIFEST_MAP (match por prefijo) → patch 404.
"""
from __future__ import annotations

from types import SimpleNamespace

from agents.sre.models import Anomaly

_POD = "whatsapp-personal-deployment-79df67f7d7-47p9k"


def _mem_result(pod=_POD, ratio="0.86"):
    return [{"metric": {"namespace": "amael-ia", "pod": pod}, "value": [0, ratio]}]


def _fake_k8s(owner_refs=None, boom=False):
    class _V1:
        def read_namespaced_pod(self, name, namespace):
            if boom:
                raise RuntimeError("apiserver caído")
            return SimpleNamespace(metadata=SimpleNamespace(owner_references=owner_refs))

    return SimpleNamespace(CoreV1Api=lambda: _V1())


def _rs(name):
    return SimpleNamespace(kind="ReplicaSet", name=name)


def _patch_prom(monkeypatch, observer, mem):
    def _q(_url, query):
        return mem if "memory" in query else []
    monkeypatch.setattr(observer, "_prometheus_query", _q)


# ── observe_metrics resuelve el dueño ─────────────────────────────────────────

def test_high_memory_anomaly_carries_deployment_owner(monkeypatch):
    from agents.sre import observer

    _patch_prom(monkeypatch, observer, _mem_result())
    monkeypatch.setattr(
        observer, "_get_k8s_client",
        lambda: _fake_k8s([_rs("whatsapp-personal-deployment-79df67f7d7")]),
    )
    [a] = [x for x in observer.observe_metrics("http://prom") if x.issue_type == "HIGH_MEMORY"]
    assert a.owner_name == "whatsapp-personal-deployment"
    assert a.metadata.get("owner_kind") == "Deployment"
    assert a.resource_name == _POD  # el pod sigue identificando el incidente


def test_job_pod_metric_anomaly_marked_as_job(monkeypatch):
    """Un pod de Job con memoria alta no debe acabar en ROLLOUT_RESTART."""
    from agents.sre import observer

    _patch_prom(monkeypatch, observer, _mem_result(pod="postgres-backup-1-abc"))
    monkeypatch.setattr(
        observer, "_get_k8s_client",
        lambda: _fake_k8s([SimpleNamespace(kind="Job", name="postgres-backup-1")]),
    )
    [a] = observer.observe_metrics("http://prom")
    assert a.metadata.get("owner_kind") == "Job"


def test_owner_lookup_failure_does_not_break_observation(monkeypatch):
    from agents.sre import observer

    _patch_prom(monkeypatch, observer, _mem_result())
    monkeypatch.setattr(observer, "_get_k8s_client", lambda: _fake_k8s(boom=True))
    [a] = observer.observe_metrics("http://prom")
    assert a.issue_type == "HIGH_MEMORY"
    assert a.owner_name == ""


# ── El healer reinicia el Deployment correcto ─────────────────────────────────

def _mem_anomaly(owner="whatsapp-personal-deployment", pod=_POD):
    return Anomaly(
        issue_type="HIGH_MEMORY", severity="HIGH", namespace="amael-ia",
        resource_name=pod, resource_type="Pod",
        details="Memoria 86.0% supera umbral 85%",
        owner_name=owner,
        metadata={"owner_kind": "Deployment"} if owner else {},
    )


def test_high_memory_with_owner_restarts_the_deployment(monkeypatch):
    from agents.sre import healer

    seen = []
    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda name, _ns: seen.append(name) or True)
    assert healer.decide_action(_mem_anomaly(), confidence=0.9) == "ROLLOUT_RESTART"
    assert seen == ["whatsapp-personal-deployment"]


def test_owner_name_is_never_renamed_by_manifest_map():
    """Regresión 5-oct: frontend-next-deployment → frontend-next → 404."""
    from agents.sre.healer import resolve_restart_target

    a = _mem_anomaly(owner="frontend-next-deployment",
                     pod="frontend-next-deployment-7974dddd85-khjg2")
    assert resolve_restart_target(a) == "frontend-next-deployment"


def test_without_owner_falls_back_to_longest_manifest_prefix():
    from agents.sre.healer import resolve_restart_target

    a = _mem_anomaly(owner="", pod="frontend-next-deployment-7974dddd85-khjg2")
    assert resolve_restart_target(a) == "frontend-next-deployment"


def test_execute_restart_patches_owner_deployment(monkeypatch):
    from agents.sre import healer

    patched = []
    monkeypatch.setattr(healer, "_check_restart_limit", lambda *_a, **_k: False, raising=False)
    monkeypatch.setattr(healer, "rollout_restart",
                        lambda name, ns: patched.append(name) or f"✅ ROLLOUT_RESTART ejecutado en {ns}/{name}")
    monkeypatch.setattr(healer, "_schedule_verification", lambda *_a, **_k: None, raising=False)
    from agents.sre import autonomy
    monkeypatch.setattr(autonomy, "apply_mode_to_action", lambda action, _c: action)
    a = _mem_anomaly(owner="frontend-next-deployment",
                     pod="frontend-next-deployment-7974dddd85-khjg2")
    healer.execute_sre_action(a, "ROLLOUT_RESTART", notify_fn=None)
    assert patched == ["frontend-next-deployment"]


# ── Dedup: un estado persistente no re-alerta cada 15 min ─────────────────────

def test_metric_anomalies_dedup_at_least_one_hour():
    from agents.sre.scheduler import _dedup_ttl_for

    for t in ("HIGH_MEMORY", "HIGH_CPU"):
        assert _dedup_ttl_for(f"amael-ia:{_POD}:{t}") >= 3600, t
