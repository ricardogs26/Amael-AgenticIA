"""
Tests de occurrence_key (P1).

sre_incidents tiene UNIQUE(incident_key) e inserta con ON CONFLICT DO NOTHING.
Con una clave sin componente temporal, la primera ocurrencia bloquea el registro
de todas las reincidencias posteriores — para siempre. occurrence_key separa
"qué falla" (estable, para Redis) de "cuándo falló" (por episodio, para Postgres).
"""
from __future__ import annotations

from datetime import UTC, datetime

from agents.sre.models import Anomaly


def _anomaly(ts: datetime) -> Anomaly:
    return Anomaly(
        issue_type="DEPLOYMENT_DEGRADED",
        severity="CRITICAL",
        namespace="amael-ia",
        resource_name="trader-service",
        resource_type="Deployment",
        details="0/1 réplicas",
        owner_name="trader-service",
        timestamp=ts,
    )


def test_incident_key_stays_stable_across_time():
    """La clave de dedup no debe llevar tiempo: Redis y approvals dependen de ella."""
    a = _anomaly(datetime(2026, 7, 28, 14, 8, tzinfo=UTC))
    b = _anomaly(datetime(2026, 8, 1, 13, 10, tzinfo=UTC))
    assert a.incident_key == b.incident_key == "amael-ia:trader-service:DEPLOYMENT_DEGRADED"


def test_occurrence_key_differs_across_windows():
    """El caso real: un incidente del 28-jul no debe bloquear el del 1-ago."""
    jul = _anomaly(datetime(2026, 7, 28, 14, 8, tzinfo=UTC))
    ago = _anomaly(datetime(2026, 8, 1, 13, 10, tzinfo=UTC))
    assert jul.occurrence_key != ago.occurrence_key


def test_occurrence_key_shared_within_same_window():
    """Dos detecciones del mismo episodio colapsan en una fila."""
    a = _anomaly(datetime(2026, 8, 1, 13, 10, tzinfo=UTC))
    b = _anomaly(datetime(2026, 8, 1, 13, 52, tzinfo=UTC))
    assert a.occurrence_key == b.occurrence_key


def test_occurrence_key_is_prefixed_by_incident_key():
    """Permite buscar todas las ocurrencias de un problema con LIKE 'incident_key%'."""
    a = _anomaly(datetime(2026, 8, 1, 13, 10, tzinfo=UTC))
    assert a.occurrence_key.startswith(a.incident_key + ":")


def test_occurrence_key_is_stable_for_one_instance():
    """Derivada de self.timestamp, no del reloj: store e update deben coincidir."""
    a = _anomaly(datetime(2026, 8, 1, 13, 10, tzinfo=UTC))
    assert a.occurrence_key == a.occurrence_key


def test_occurrence_key_respects_window_env(monkeypatch):
    from agents.sre import models

    monkeypatch.setattr(models, "SRE_OCCURRENCE_WINDOW_MIN", 1, raising=False)
    a = _anomaly(datetime(2026, 8, 1, 13, 10, 0, tzinfo=UTC))
    b = _anomaly(datetime(2026, 8, 1, 13, 11, 0, tzinfo=UTC))
    assert a.occurrence_key != b.occurrence_key


# ── Cableado: qué clave llega a Postgres y cuál a Redis ──────────────────────

def test_verification_job_splits_keys(monkeypatch):
    """update_incident_fn recibe occurrence_key; Redis sigue viendo incident_key."""
    from agents.sre import healer

    updated: list[str] = []
    redis_lookups: list[str] = []

    monkeypatch.setattr(healer, "_is_deployment_healthy", lambda *_a: True)
    monkeypatch.setattr(
        healer, "_get_rfc_from_redis",
        lambda k: redis_lookups.append(k) or None,
    )
    monkeypatch.setattr(healer, "_update_camael_gitops_status", lambda *_a, **_kw: None)

    healer._run_verification_job(
        incident_key="amael-ia:trader-service:DEPLOYMENT_DEGRADED",
        deployment_name="trader-service",
        namespace="amael-ia",
        update_incident_fn=lambda key, _result: updated.append(key),
        notify_fn=None,
        generate_postmortem_fn=None,
        occurrence_key="amael-ia:trader-service:DEPLOYMENT_DEGRADED:20260801T1300",
    )

    assert updated == ["amael-ia:trader-service:DEPLOYMENT_DEGRADED:20260801T1300"]
    assert redis_lookups == ["amael-ia:trader-service:DEPLOYMENT_DEGRADED"]


def test_verification_job_defaults_occurrence_to_incident_key(monkeypatch):
    """Sin occurrence_key explícita, el comportamiento es el de antes."""
    from agents.sre import healer

    updated: list[str] = []
    monkeypatch.setattr(healer, "_is_deployment_healthy", lambda *_a: True)
    monkeypatch.setattr(healer, "_get_rfc_from_redis", lambda _k: None)
    monkeypatch.setattr(healer, "_update_camael_gitops_status", lambda *_a, **_kw: None)

    healer._run_verification_job(
        incident_key="ns:res:TYPE",
        deployment_name="res",
        namespace="ns",
        update_incident_fn=lambda key, _result: updated.append(key),
        notify_fn=None,
        generate_postmortem_fn=None,
    )
    assert updated == ["ns:res:TYPE"]
