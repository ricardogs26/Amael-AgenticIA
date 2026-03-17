"""
SRE Healer — decide y ejecuta acciones de remediación.

Migrado desde k8s-agent/main.py:
  decide_action()           — evalúa guardrails y decide la acción (P0)
  execute_sre_action()      — ejecuta ROLLOUT_RESTART o NOTIFY_HUMAN
  rollout_restart()         — kubectl rollout restart via K8s API
  rollout_undo_deployment() — kubectl rollout undo (P5-B)
  _run_verification_job()   — verificación post-acción 5min después (P3-A)
  _schedule_verification()  — schedula el job de verificación
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import UTC

from agents.sre.models import Anomaly
from core.constants import ActionType, Severity

logger = logging.getLogger("agents.sre.healer")

_DEFAULT_NAMESPACE       = os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
_PROTECTED_CSV           = os.environ.get(
    "SRE_PROTECTED_DEPLOYMENTS", "postgres-deployment,ollama-deployment,vault-0"
)
PROTECTED_DEPLOYMENTS    = {d.strip() for d in _PROTECTED_CSV.split(",") if d.strip()}
AUTO_HEAL_MIN_SEVERITY   = os.environ.get("SRE_AUTO_HEAL_MIN_SEVERITY", "HIGH")
CONFIDENCE_THRESHOLD     = float(os.environ.get("SRE_CONFIDENCE_THRESHOLD", "0.75"))
_SEVERITY_RANK           = {
    Severity.LOW: 0, Severity.MEDIUM: 1,
    Severity.HIGH: 2, Severity.CRITICAL: 3,
}
_AUTO_HEALABLE_ISSUES = {
    "CRASH_LOOP", "OOM_KILLED", "POD_FAILED",
    "HIGH_RESTARTS", "HIGH_MEMORY", "MEMORY_LEAK_PREDICTED",
}


def _get_k8s_apps():
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api()


def decide_action(anomaly: Anomaly, confidence: float) -> str:
    """
    Aplica guardrails y decide la acción apropiada.

    Reglas (por orden de evaluación):
      1. Recurso protegido → NOTIFY_HUMAN
      2. Tipo no auto-healable → NOTIFY_HUMAN
      3. Severidad insuficiente → NO_ACTION
      4. Confianza insuficiente → NOTIFY_HUMAN
      5. IMAGE_PULL_ERROR / NODE_NOT_READY / POD_PENDING_STUCK → NOTIFY_HUMAN
      6. Cumple todos los criterios → ROLLOUT_RESTART

    Migrado desde k8s-agent/main.py → decide_action()
    """
    resource = anomaly.owner_name or anomaly.resource_name

    if resource in PROTECTED_DEPLOYMENTS:
        logger.info(f"[healer] '{resource}' protegido → NOTIFY_HUMAN")
        return ActionType.NOTIFY_HUMAN

    if anomaly.issue_type not in _AUTO_HEALABLE_ISSUES:
        return ActionType.NOTIFY_HUMAN

    sev_rank  = _SEVERITY_RANK.get(anomaly.severity, 0)
    min_rank  = _SEVERITY_RANK.get(AUTO_HEAL_MIN_SEVERITY, 2)
    if sev_rank < min_rank:
        return ActionType.NO_ACTION

    if confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            f"[healer] Confianza {confidence:.2f} < {CONFIDENCE_THRESHOLD} → NOTIFY_HUMAN"
        )
        return ActionType.NOTIFY_HUMAN

    return ActionType.ROLLOUT_RESTART


def rollout_restart(deployment_name: str, namespace: str) -> str:
    """
    Ejecuta rollout restart del deployment via K8s API.
    Equivalente a: kubectl rollout restart deployment/<name> -n <ns>
    """
    from datetime import datetime
    try:
        apps_v1 = _get_k8s_apps()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt":
                                datetime.now(UTC).isoformat()
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch,
        )
        msg = f"✅ ROLLOUT_RESTART ejecutado en {namespace}/{deployment_name}"
        logger.info(f"[healer] {msg}")
        return msg
    except Exception as exc:
        msg = f"❌ Error en ROLLOUT_RESTART {namespace}/{deployment_name}: {exc}"
        logger.error(f"[healer] {msg}")
        return msg


def _was_recently_deployed(deployment_name: str, namespace: str, window_minutes: int = 30) -> bool:
    """True si el deployment tuvo una actualización en los últimos window_minutes minutos."""
    try:
        apps_v1 = _get_k8s_apps()
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        for cond in (dep.status.conditions or []):
            if cond.type == "Progressing" and cond.last_update_time:
                from datetime import datetime
                age = (
                    datetime.now(UTC)
                    - cond.last_update_time.replace(tzinfo=UTC)
                ).total_seconds()
                if age < window_minutes * 60:
                    return True
    except Exception as exc:
        logger.warning(f"[healer] _was_recently_deployed error: {exc}")
    return False


def rollout_undo_deployment(deployment_name: str, namespace: str) -> str:
    """
    Auto-rollback: deshace el último rollout del deployment (P5-B).
    Equivalente a: kubectl rollout undo deployment/<name> -n <ns>
    """
    try:
        apps_v1 = _get_k8s_apps()
        # Obtener ReplicaSets del deployment para encontrar la revisión anterior
        rs_list = apps_v1.list_namespaced_replica_set(
            namespace=namespace,
        )
        # Filtrar RS del deployment y ordenar por revision
        dep_rs = [
            rs for rs in rs_list.items
            if any(
                ref.name == deployment_name
                for ref in (rs.metadata.owner_references or [])
            )
        ]
        if len(dep_rs) < 2:
            return f"⚠️ No hay revisión anterior para rollback de {deployment_name}"

        dep_rs.sort(
            key=lambda r: int(
                (r.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision", "0"
                )
            )
        )
        # La penúltima RS es la revisión anterior
        prev_rs = dep_rs[-2]
        prev_revision = (prev_rs.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision", "?"
        )

        patch = {
            "spec": {
                "template": prev_rs.spec.template.to_dict()
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch,
        )
        msg = (
            f"✅ ROLLBACK ejecutado en {namespace}/{deployment_name} "
            f"→ revisión {prev_revision}"
        )
        logger.info(f"[healer] {msg}")
        return msg
    except Exception as exc:
        msg = f"❌ Error en ROLLBACK {namespace}/{deployment_name}: {exc}"
        logger.error(f"[healer] {msg}")
        return msg


def _is_deployment_healthy(deployment_name: str, namespace: str) -> bool:
    """Verifica si las réplicas deseadas están todas disponibles."""
    try:
        apps_v1 = _get_k8s_apps()
        dep = apps_v1.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
        desired   = dep.spec.replicas or 1
        available = dep.status.available_replicas or 0
        return available >= desired
    except Exception as exc:
        logger.warning(f"[healer] _is_deployment_healthy error: {exc}")
        return False


def _run_verification_job(
    incident_key: str,
    deployment_name: str,
    namespace: str,
    update_incident_fn,
    notify_fn,
    generate_postmortem_fn,
) -> None:
    """
    Job de verificación post-acción ejecutado 5 minutos después del ROLLOUT_RESTART (P3-A).
    Si el deployment sigue unhealthy + fue recientemente desplegado → auto-rollback (P5-B).
    Si healthy → genera postmortem (P5-D).
    """
    logger.info(
        f"[healer] Verificando {namespace}/{deployment_name} "
        f"(incident={incident_key})"
    )
    healthy = _is_deployment_healthy(deployment_name, namespace)

    if healthy:
        logger.info(f"[healer] ✅ {deployment_name} verificado como saludable.")
        update_incident_fn(incident_key, "verify:ok")
        if generate_postmortem_fn:
            threading.Thread(
                target=generate_postmortem_fn,
                args=(incident_key,),
                daemon=True,
            ).start()
    elif _was_recently_deployed(deployment_name, namespace):
        logger.warning(
            f"[healer] {deployment_name} sigue unhealthy + recién desplegado → auto-rollback"
        )
        from observability.metrics import SRE_ROLLBACK_TOTAL
        rollback_result = rollout_undo_deployment(deployment_name, namespace)
        if "✅" in rollback_result:
            SRE_ROLLBACK_TOTAL.labels(result="ok").inc()
            update_incident_fn(incident_key, "verify:rollback:ok")
        else:
            SRE_ROLLBACK_TOTAL.labels(result="error").inc()
            update_incident_fn(incident_key, "verify:rollback:error")
        if notify_fn:
            notify_fn(
                f"⚠️ AUTO-ROLLBACK en {namespace}/{deployment_name}: {rollback_result}",
                "HIGH",
            )
    else:
        logger.warning(f"[healer] {deployment_name} sigue unhealthy pero sin deploy reciente.")
        update_incident_fn(incident_key, "verify:unresolved")
        if notify_fn:
            notify_fn(
                f"🚨 {namespace}/{deployment_name} SIGUE UNHEALTHY tras ROLLOUT_RESTART. "
                "Intervención manual requerida.",
                "CRITICAL",
            )


def schedule_verification(
    incident_key: str,
    deployment_name: str,
    namespace: str,
    scheduler,
    update_incident_fn,
    notify_fn,
    generate_postmortem_fn=None,
    delay_seconds: int = 300,
) -> None:
    """
    Schedula el job de verificación post-acción con APScheduler (P3-A).
    Se ejecuta delay_seconds después de la acción (default: 5 minutos).
    """
    try:
        from datetime import datetime, timedelta
        run_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        scheduler.add_job(
            _run_verification_job,
            "date",
            run_date=run_at,
            args=[
                incident_key, deployment_name, namespace,
                update_incident_fn, notify_fn, generate_postmortem_fn,
            ],
            id=f"verify_{incident_key}",
            replace_existing=True,
        )
        logger.info(
            f"[healer] Verificación schedulada para {namespace}/{deployment_name} "
            f"en {delay_seconds}s (incident={incident_key})"
        )
    except Exception as exc:
        logger.warning(f"[healer] No se pudo schedulear verificación: {exc}")


def execute_sre_action(
    anomaly: Anomaly,
    action_type: str,
    notify_fn,
) -> str:
    """
    Ejecuta la acción decidida por decide_action().

    Args:
        anomaly:     La anomalía a remediar.
        action_type: ActionType (ROLLOUT_RESTART, NOTIFY_HUMAN, NO_ACTION).
        notify_fn:   Función para notificar por WhatsApp.

    Returns:
        Descripción del resultado de la acción.
    """
    from observability.metrics import SRE_ACTIONS_TAKEN_TOTAL

    if action_type == ActionType.NO_ACTION:
        return "NO_ACTION: No se requiere intervención."

    if action_type == ActionType.NOTIFY_HUMAN:
        msg = (
            f"🔔 SRE ALERTA [{anomaly.severity}]\n"
            f"Tipo: {anomaly.issue_type}\n"
            f"Recurso: {anomaly.namespace}/{anomaly.resource_name}\n"
            f"Detalles: {anomaly.details[:200]}"
        )
        if notify_fn:
            notify_fn(msg, anomaly.severity)
        SRE_ACTIONS_TAKEN_TOTAL.labels(action=action_type, result="notified").inc()
        return f"NOTIFY_HUMAN: Alerta enviada para {anomaly.resource_name}"

    if action_type == ActionType.ROLLOUT_RESTART:
        target = anomaly.owner_name or anomaly.resource_name
        namespace = anomaly.namespace

        from agents.sre.healer import _check_restart_limit
        if _check_restart_limit(target, namespace):
            from observability.metrics import SRE_RESTART_LIMIT_HIT
            SRE_RESTART_LIMIT_HIT.inc()
            logger.warning(f"[healer] Límite de reinicios alcanzado para {target}")
            if notify_fn:
                notify_fn(
                    f"⚠️ Límite de reinicios alcanzado para {namespace}/{target}. "
                    "Intervención manual requerida.",
                    "HIGH",
                )
            SRE_ACTIONS_TAKEN_TOTAL.labels(action=action_type, result="limit_hit").inc()
            return f"LIMIT_HIT: Límite de reinicios para {target}"

        result = rollout_restart(target, namespace)
        status = "ok" if "✅" in result else "error"
        SRE_ACTIONS_TAKEN_TOTAL.labels(action=action_type, result=status).inc()
        return result

    return f"Acción desconocida: {action_type}"


def _check_restart_limit(resource_name: str, namespace: str) -> bool:
    """True si el recurso alcanzó MAX_RESTARTS_PER_RESOURCE en la ventana."""
    try:
        from storage.redis import get_client
        redis = get_client()
        max_r = int(os.environ.get("SRE_MAX_RESTARTS_PER_RESOURCE", "3"))
        key   = f"sre:restarts:{namespace}:{resource_name}"
        count = int(redis.get(key) or 0)
        return count >= max_r
    except Exception:
        return False


def record_restart(resource_name: str, namespace: str) -> None:
    """Incrementa el contador de reinicios automáticos (con TTL de ventana)."""
    try:
        from storage.redis import get_client
        redis = get_client()
        window = int(os.environ.get("SRE_RESTART_WINDOW_MINUTES", "15"))
        key = f"sre:restarts:{namespace}:{resource_name}"
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window * 60)
        pipe.execute()
    except Exception as exc:
        logger.warning(f"[healer] record_restart error: {exc}")
