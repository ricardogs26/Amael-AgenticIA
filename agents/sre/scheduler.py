"""
SRE Scheduler — loop autónomo de observación, detección y remediación.

Migrado desde k8s-agent/main.py:
  sre_autonomous_loop()  — ciclo completo Observe→Detect→Diagnose→Decide→Act→Report
  CircuitBreaker         — corta el loop tras N errores consecutivos (P0)
  Maintenance window     — pausa el loop durante mantenimiento planificado (P4-C)
  Leader election        — Kubernetes Lease para single-leader en multi-pod (P3-D)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from agents.sre.models import SRELoopState

logger = logging.getLogger("agents.sre.scheduler")

_POD_NAME          = os.environ.get("POD_NAME", "k8s-agent-single")
_LEASE_NAME        = os.environ.get("SRE_LEASE_NAME", "sre-agent-leader")
_LEASE_NAMESPACE   = os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
_LEASE_DURATION_S  = 90
_MAINTENANCE_KEY   = "sre:maintenance:active"


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Corta el loop autónomo tras N errores consecutivos (P0).

    Estados:
      CLOSED    — operación normal
      OPEN      — cortado, no ejecutar
      HALF_OPEN — probando si el sistema se recuperó
    """

    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, threshold: int = 5, reset_seconds: int = 300):
        self.threshold      = threshold
        self.reset_seconds  = reset_seconds
        self._failures      = 0
        self._state         = self.CLOSED
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.time() - (self._opened_at or 0) > self.reset_seconds:
                self._state = self.HALF_OPEN
                logger.info("[circuit_breaker] OPEN → HALF_OPEN (probando recuperación)")
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        prev = self._state
        self._state = self.CLOSED
        if prev != self.CLOSED:
            logger.info(f"[circuit_breaker] {prev} → CLOSED")
        from observability.metrics import SRE_CIRCUIT_BREAKER_STATE
        SRE_CIRCUIT_BREAKER_STATE.set(0)

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._state == self.CLOSED:
            self._state    = self.OPEN
            self._opened_at = time.time()
            logger.error(
                f"[circuit_breaker] CLOSED → OPEN tras {self._failures} fallos"
            )
            from observability.metrics import SRE_CIRCUIT_BREAKER_STATE
            SRE_CIRCUIT_BREAKER_STATE.set(1)

    def is_open(self) -> bool:
        return self.state == self.OPEN


# Instancia global del circuit breaker
circuit_breaker = CircuitBreaker(
    threshold=int(os.environ.get("SRE_CB_THRESHOLD", "5")),
    reset_seconds=int(os.environ.get("SRE_CB_RESET_SECONDS", "300")),
)

# Estado del loop
_loop_state = SRELoopState(
    loop_enabled=os.environ.get("SRE_LOOP_ENABLED", "true").lower() == "true",
    loop_interval_seconds=int(os.environ.get("SRE_LOOP_INTERVAL", "60")),
    last_run_at=None,
    last_run_result="pending",
    anomalies_in_last_run=0,
    actions_in_last_run=0,
    circuit_breaker_state=CircuitBreaker.CLOSED,
    maintenance_active=False,
    leader_pod="",
    is_leader=False,
)


# ── Leader Election ───────────────────────────────────────────────────────────

def _try_acquire_lease() -> bool:
    """
    Intenta adquirir el Kubernetes Lease para elección de líder (P3-D).
    Retorna True si este pod es el líder actual.
    """
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        coord = client.CoordinationV1Api()
        now   = datetime.now(UTC)

        try:
            lease = coord.read_namespaced_lease(
                name=_LEASE_NAME, namespace=_LEASE_NAMESPACE
            )
            holder  = lease.spec.holder_identity or ""
            renewed = lease.spec.renew_time
            expired = (
                renewed is None
                or (now - renewed.replace(tzinfo=UTC)).total_seconds()
                > _LEASE_DURATION_S
            )
            if holder == _POD_NAME or expired:
                lease.spec.holder_identity = _POD_NAME
                lease.spec.renew_time      = now
                coord.replace_namespaced_lease(
                    name=_LEASE_NAME, namespace=_LEASE_NAMESPACE, body=lease
                )
                _loop_state.leader_pod = _POD_NAME
                _loop_state.is_leader  = True
                return True
            else:
                _loop_state.leader_pod = holder
                _loop_state.is_leader  = False
                return False
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Crear el Lease
                body = client.V1Lease(
                    metadata=client.V1ObjectMeta(
                        name=_LEASE_NAME, namespace=_LEASE_NAMESPACE
                    ),
                    spec=client.V1LeaseSpec(
                        holder_identity=_POD_NAME,
                        lease_duration_seconds=_LEASE_DURATION_S,
                        renew_time=now,
                    ),
                )
                coord.create_namespaced_lease(namespace=_LEASE_NAMESPACE, body=body)
                _loop_state.leader_pod = _POD_NAME
                _loop_state.is_leader  = True
                return True
            raise
    except Exception as exc:
        logger.warning(f"[scheduler] Leader election error: {exc}. Asumiendo líder.")
        return True


# ── Maintenance Window ─────────────────────────────────────────────────────────

def is_maintenance_active() -> bool:
    """Verifica si hay una ventana de mantenimiento activa en Redis."""
    try:
        from storage.redis import get_client
        active = get_client().exists(_MAINTENANCE_KEY) == 1
        _loop_state.maintenance_active = active
        return active
    except Exception:
        return False


def activate_maintenance(minutes: int) -> str:
    """Activa una ventana de mantenimiento por N minutos."""
    try:
        from storage.redis import get_client
        get_client().setex(_MAINTENANCE_KEY, minutes * 60, "1")
        from observability.metrics import SRE_MAINTENANCE_ACTIVE
        SRE_MAINTENANCE_ACTIVE.set(1)
        return f"✅ Mantenimiento activado por {minutes} minutos"
    except Exception as exc:
        return f"❌ Error activando mantenimiento: {exc}"


def deactivate_maintenance() -> str:
    """Desactiva la ventana de mantenimiento activa."""
    try:
        from storage.redis import get_client
        get_client().delete(_MAINTENANCE_KEY)
        from observability.metrics import SRE_MAINTENANCE_ACTIVE
        SRE_MAINTENANCE_ACTIVE.set(0)
        return "✅ Mantenimiento desactivado"
    except Exception as exc:
        return f"❌ Error desactivando mantenimiento: {exc}"


# ── Deduplicación de incidentes ───────────────────────────────────────────────

_DEDUP_TTL = 600  # 10 minutos
_dedup_cache: dict[str, float] = {}  # fallback in-memory


def is_duplicate_incident(key: str) -> bool:
    """Verifica si el incidente ya fue procesado recientemente."""
    try:
        from storage.redis import get_client
        return get_client().exists(f"sre:incident:{key}") == 1
    except Exception:
        now = time.time()
        if key in _dedup_cache and now - _dedup_cache[key] < _DEDUP_TTL:
            return True
        _dedup_cache.pop(key, None)
        return False


def mark_incident(key: str) -> None:
    """Marca el incidente como procesado para deduplicación."""
    try:
        from storage.redis import get_client
        get_client().set(f"sre:incident:{key}", "1", ex=_DEDUP_TTL)
    except Exception:
        _dedup_cache[key] = time.time()


# ── SRE Autonomous Loop ───────────────────────────────────────────────────────

def sre_autonomous_loop(
    prometheus_url: str,
    slo_targets: list[dict],
    vault_knowledge: str = "",
    metrics_knowledge: str = "",
) -> None:
    """
    Loop principal autónomo del SRE Agent.
    Ejecutado cada SRE_LOOP_INTERVAL segundos por APScheduler.

    Pipeline: Observe → Detect → Diagnose → Decide → Act → Report
    """
    from agents.sre import detector, diagnoser, healer, observer, reporter
    from observability.metrics import SRE_LOOP_RUNS_TOTAL

    # ── Guardrails de ejecución ───────────────────────────────────────────────
    if circuit_breaker.is_open():
        logger.warning("[scheduler] Circuit breaker OPEN. Skipping loop.")
        SRE_LOOP_RUNS_TOTAL.labels(result="skipped_cb").inc()
        return

    if is_maintenance_active():
        logger.info("[scheduler] Ventana de mantenimiento activa. Skipping loop.")
        SRE_LOOP_RUNS_TOTAL.labels(result="maintenance").inc()
        return

    if not _try_acquire_lease():
        logger.debug(
            f"[scheduler] No soy líder (líder: {_loop_state.leader_pod}). Skipping."
        )
        SRE_LOOP_RUNS_TOTAL.labels(result="skipped_not_leader").inc()
        return

    _loop_state.last_run_at = datetime.now(UTC)
    actions_taken = 0

    try:
        # ── OBSERVE ──────────────────────────────────────────────────────────
        structural = observer.observe_cluster()
        metrics    = observer.observe_metrics(prometheus_url)
        trends     = observer.observe_trends(prometheus_url)
        slo_anoms  = observer.observe_slo(prometheus_url, slo_targets)

        # ── DETECT ───────────────────────────────────────────────────────────
        raw_anomalies = detector.detect_anomalies(
            structural=structural,
            metric=metrics,
            trend=trends,
            slo=slo_anoms,
        )
        anomalies = detector.correlate_anomalies(raw_anomalies)

        _loop_state.anomalies_in_last_run = len(anomalies)

        if not anomalies:
            _loop_state.last_run_result = "ok_clean"
            SRE_LOOP_RUNS_TOTAL.labels(result="ok_clean").inc()
            circuit_breaker.record_success()
            return

        # ── DIAGNOSE + DECIDE + ACT ───────────────────────────────────────────
        for anomaly in anomalies:
            if is_duplicate_incident(anomaly.incident_key):
                logger.debug(f"[scheduler] Incidente duplicado: {anomaly.incident_key}")
                continue

            # Diagnose
            root_cause, confidence = diagnoser.diagnose_with_llm(
                anomaly, vault_knowledge, metrics_knowledge
            )
            confidence = diagnoser.adjust_confidence_with_history(
                anomaly, confidence, reporter.get_historical_success_rate
            )
            anomaly.confidence = confidence
            anomaly.root_cause  = root_cause

            # Decide
            action_type = healer.decide_action(anomaly, confidence)

            # Act
            action_result = healer.execute_sre_action(
                anomaly, action_type, reporter.notify_whatsapp_sre
            )
            if action_type == "ROLLOUT_RESTART":
                healer.record_restart(
                    anomaly.owner_name or anomaly.resource_name, anomaly.namespace
                )

            # Report
            mark_incident(anomaly.incident_key)
            reporter.store_incident(
                incident_key=anomaly.incident_key,
                namespace=anomaly.namespace,
                resource_name=anomaly.resource_name,
                resource_type=anomaly.resource_type,
                issue_type=anomaly.issue_type,
                severity=anomaly.severity,
                details=anomaly.details,
                root_cause=root_cause,
                confidence=confidence,
                action_taken=action_type,
                action_result=action_result,
                notified=action_type == "NOTIFY_HUMAN",
            )

            # Schedule verification post-acción (solo ROLLOUT_RESTART)
            if action_type == "ROLLOUT_RESTART" and "✅" in action_result:
                # Nota: el scheduler se pasa desde el agent.py
                diagnoser.maybe_save_runbook_entry(anomaly, root_cause, action_type)

            actions_taken += 1

        _loop_state.actions_in_last_run = actions_taken
        _loop_state.last_run_result = "ok"
        SRE_LOOP_RUNS_TOTAL.labels(result="ok").inc()
        circuit_breaker.record_success()

    except Exception as exc:
        logger.error(f"[scheduler] Error en SRE loop: {exc}", exc_info=True)
        _loop_state.last_run_result = "error"
        SRE_LOOP_RUNS_TOTAL.labels(result="error").inc()
        circuit_breaker.record_failure()


def get_loop_state() -> SRELoopState:
    """Retorna el estado actual del loop para el endpoint /api/sre/loop/status."""
    _loop_state.circuit_breaker_state = circuit_breaker.state
    _loop_state.maintenance_active    = is_maintenance_active()
    return _loop_state
