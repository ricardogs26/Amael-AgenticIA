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
                try:
                    lease.spec.holder_identity = _POD_NAME
                    lease.spec.renew_time      = now
                    coord.replace_namespaced_lease(
                        name=_LEASE_NAME, namespace=_LEASE_NAMESPACE, body=lease
                    )
                    _loop_state.leader_pod = _POD_NAME
                    _loop_state.is_leader  = True
                    return True
                except client.exceptions.ApiException as e409:
                    if e409.status == 409:
                        # Otro proceso actualizó el lease primero (race) — no soy líder
                        _loop_state.is_leader = False
                        return False
                    raise
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

# TTL por tipo de anomalía.
# HIGH_RESTARTS y MEMORY_LEAK_PREDICTED usan TTL largo porque, incluso cuando
# son reales, no requieren re-alertar en minutos — ya hay una acción en curso.
# Anomalías activas (CRASH_LOOP, OOM, IMAGE_PULL) usan TTL corto para que
# vuelvan a procesarse si el problema no se resuelve.
_DEDUP_TTL_DEFAULT = 600       # 10 min — anomalías activas
_DEDUP_TTL_BY_TYPE: dict[str, int] = {
    "HIGH_RESTARTS":         3600,   # 1 hora — solo alerta cuando hay crecimiento real
    "MEMORY_LEAK_PREDICTED": 3600,   # 1 hora — tendencia, no emergencia inmediata
    "SLO_BUDGET_BURNING":    1800,   # 30 min — importante pero no urgente por segundo
    "HIGH_CPU":               900,   # 15 min
    "HIGH_MEMORY":            900,   # 15 min
    # Infraestructura — TTL largo para no repetir notificaciones de estado persistente
    "VAULT_SEALED":          1800,   # 30 min — persiste hasta unseal manual
    "LOADBALANCER_NO_IP":    1800,   # 30 min — persiste hasta fix de MetalLB
    "PVC_PENDING":           1800,   # 30 min — persiste hasta provisionar
    "DEPLOYMENT_DEGRADED":    300,   # 5 min — puede cambiar rápido post-restart
    "SERVICE_NO_ENDPOINTS":   600,   # 10 min
    "NODE_PRESSURE":         1800,   # 30 min — condición de nodo persiste
    "K8S_EVENT_WARNING":      300,   # 5 min — eventos se repiten
    "PVC_MOUNT_ERROR":        600,   # 10 min
    # P7: capacidad proactiva
    "NODE_DISK_HIGH":        1800,   # 30 min — estado del disco cambia lentamente
    "NODE_MEMORY_HIGH":       900,   # 15 min — puede resolverse con GC de pods
    "PVC_CAPACITY_HIGH":     1800,   # 30 min — persiste hasta limpiar/ampliar
    # P7: certificados TLS
    "CERTIFICATE_EXPIRING":  3600,   # 1 hora — alertar una vez por hora hasta resolver
}

_dedup_cache: dict[str, float] = {}  # fallback in-memory


def _dedup_ttl_for(key: str) -> int:
    """Retorna el TTL de dedup apropiado según el tipo de anomalía en la clave."""
    # La clave tiene formato "namespace:resource_name:ISSUE_TYPE"
    issue_type = key.rsplit(":", 1)[-1] if ":" in key else key
    return _DEDUP_TTL_BY_TYPE.get(issue_type, _DEDUP_TTL_DEFAULT)


def is_duplicate_incident(key: str) -> bool:
    """Verifica si el incidente ya fue procesado recientemente."""
    try:
        from storage.redis import get_client
        return get_client().exists(f"sre:incident:{key}") == 1
    except Exception:
        now = time.time()
        ttl = _dedup_ttl_for(key)
        if key in _dedup_cache and now - _dedup_cache[key] < ttl:
            return True
        _dedup_cache.pop(key, None)
        return False


def mark_incident(key: str) -> None:
    """Marca el incidente como procesado para deduplicación."""
    ttl = _dedup_ttl_for(key)
    try:
        from storage.redis import get_client
        get_client().set(f"sre:incident:{key}", "1", ex=ttl)
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
        structural   = observer.observe_cluster()
        infra        = observer.observe_infrastructure()
        metrics      = observer.observe_metrics(prometheus_url)
        trends       = observer.observe_trends(prometheus_url)
        slo_anoms    = observer.observe_slo(prometheus_url, slo_targets)
        # P7: capacidad de nodo/PVC y certificados TLS
        node_res     = observer.observe_node_resources(prometheus_url)
        pvc_cap      = observer.observe_pvc_capacity(prometheus_url)
        certs        = observer.observe_certificates()

        # Las observaciones de P7 se agregan a infrastructure para reutilizar
        # el pipeline de detect/diagnose/decide sin cambiar la firma de detect_anomalies.
        infra_full   = infra + node_res + pvc_cap + certs

        # ── DETECT ───────────────────────────────────────────────────────────
        raw_anomalies = detector.detect_anomalies(
            structural=structural,
            infrastructure=infra_full,
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
        # Dedup a nivel de loop: solo un handoff GitOps por deployment por ejecución.
        # Previene múltiples PRs cuando el mismo pod genera varias anomalías en el
        # mismo ciclo (p.ej. OOM_KILLED + DEPLOYMENT_DEGRADED simultáneos).
        _gitops_dispatched_this_run: set[str] = set()

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
                _restart_target = anomaly.owner_name or anomaly.resource_name
                # Normalize pod name → deployment name so _check_restart_limit matches
                from agents.sre.bug_library import APP_MANIFEST_MAP
                for _map_key in APP_MANIFEST_MAP:
                    if _restart_target != _map_key and _restart_target.startswith(_map_key + "-"):
                        _restart_target = _map_key
                        break
                healer.record_restart(_restart_target, anomaly.namespace)

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
                diagnoser.maybe_save_runbook_entry(anomaly, root_cause, action_type)

                # Normalizar nombre de deployment (strip pod hash suffix via APP_MANIFEST_MAP)
                raw_deploy = anomaly.owner_name or anomaly.resource_name
                from agents.sre.bug_library import APP_MANIFEST_MAP
                _deploy_normalized = raw_deploy
                for _key in APP_MANIFEST_MAP:
                    if raw_deploy == _key or raw_deploy.startswith(_key + "-"):
                        _deploy_normalized = _key
                        break

                # GitOps handoff — solo UNO por deployment por ciclo (loop-level dedup)
                _deploy_loop_key = f"{anomaly.namespace}:{_deploy_normalized}"
                if _deploy_loop_key not in _gitops_dispatched_this_run:
                    healer.handoff_to_camael(anomaly, anomaly.incident_key, reporter.notify_whatsapp_sre)
                    _gitops_dispatched_this_run.add(_deploy_loop_key)
                else:
                    logger.info(
                        f"[scheduler] GitOps handoff omitido (loop-dedup): "
                        f"{_deploy_loop_key} ya tiene handoff en este ciclo "
                        f"({anomaly.issue_type})"
                    )

                # Verificación post-acción — usar nombre normalizado para que
                # _has_pending_gitops_pr encuentre la clave Redis correcta
                healer.schedule_verification(
                    incident_key=anomaly.incident_key,
                    deployment_name=_deploy_normalized,
                    namespace=anomaly.namespace,
                    update_incident_fn=reporter.update_incident_verification,
                    notify_fn=reporter.notify_whatsapp_sre,
                    generate_postmortem_fn=reporter.generate_and_store_postmortem,
                )

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
