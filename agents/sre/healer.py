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
from datetime import UTC, datetime

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
_VERIFICATION_DELAY_S    = int(os.environ.get("SRE_VERIFICATION_DELAY", "300"))
_SEVERITY_RANK           = {
    Severity.LOW: 0, Severity.MEDIUM: 1,
    Severity.HIGH: 2, Severity.CRITICAL: 3,
}
_AUTO_HEALABLE_ISSUES = {
    "CRASH_LOOP", "OOM_KILLED", "POD_FAILED",
    "HIGH_RESTARTS", "HIGH_MEMORY", "MEMORY_LEAK_PREDICTED",
    "DEPLOYMENT_DEGRADED",   # réplicas < deseadas → rollout restart del deployment
}
# Tipos que siempre van a NOTIFY_HUMAN — requieren intervención manual.
# NODE_DISK_HIGH / NODE_MEMORY_HIGH / PVC_CAPACITY_HIGH / CERTIFICATE_EXPIRING
# no tienen remediación automática válida: ampliar disco, limpiar PVC o renovar
# un certificado con problemas de ACME requiere decisión humana.
_NOTIFY_ONLY_ISSUES = {
    "NODE_DISK_HIGH", "NODE_MEMORY_HIGH",
    "PVC_CAPACITY_HIGH", "CERTIFICATE_EXPIRING",
    # Los siguientes ya eran NOTIFY_HUMAN por no estar en _AUTO_HEALABLE_ISSUES,
    # se listan explícitamente para claridad en la documentación:
    "IMAGE_PULL_ERROR", "POD_PENDING_STUCK", "HIGH_CPU",
    "DISK_EXHAUSTION_PREDICTED", "ERROR_RATE_ESCALATING",
    "SLO_BUDGET_BURNING", "SERVICE_NO_ENDPOINTS", "LOADBALANCER_NO_IP",
    "PVC_PENDING", "PVC_MOUNT_ERROR", "NODE_PRESSURE",
    "K8S_EVENT_WARNING", "VAULT_SEALED", "NODE_NOT_READY",
}


def _get_k8s_apps():
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api()


def _get_pod_logs(resource_name: str, namespace: str, lines: int = 50) -> str:
    """
    Obtiene las últimas N líneas de logs del pod más reciente del deployment.
    Busca por label app={resource_name}, luego por nombre de pod.
    Retorna string vacío si falla (no bloquea el handoff).
    Limita a 2000 caracteres para no inflar el prompt LLM.
    """
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        v1 = client.CoreV1Api()

        # Intentar por label selector primero
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={resource_name}",
        )
        # Fallback: buscar por nombre de pod
        if not pods.items:
            all_pods = v1.list_namespaced_pod(namespace=namespace)
            pods_items = [
                p for p in all_pods.items
                if resource_name in (p.metadata.name or "")
            ]
        else:
            pods_items = pods.items

        if not pods_items:
            return ""

        # Tomar el pod más reciente
        _epoch = datetime.min.replace(tzinfo=UTC)
        pod = sorted(
            pods_items,
            key=lambda p: p.metadata.creation_timestamp or _epoch,
            reverse=True,
        )[0]
        pod_name = pod.metadata.name

        # Intentar logs del contenedor anterior (el que crasheó), luego el actual
        for previous in (True, False):
            try:
                log = v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    tail_lines=lines,
                    previous=previous,
                    _request_timeout=5,
                )
                if log:
                    return log[:2000]
            except Exception:
                continue

        return ""
    except Exception as exc:
        logger.debug(f"[healer] _get_pod_logs({resource_name}): {exc}")
        return ""


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

    # Intentar leer RFC de ServiceNow desde Redis (creado por Camael en gitops_fix)
    rfc_info = _get_rfc_from_redis(incident_key)

    if healthy:
        logger.info(f"[healer] ✅ {deployment_name} verificado como saludable.")
        update_incident_fn(incident_key, "verify:ok")
        _update_camael_gitops_status(incident_key, "CLOSED", "HEALTHY")
        # Cerrar RFC en ServiceNow → Closed
        if rfc_info:
            _close_rfc_async(rfc_info, deployment_name, namespace, success=True)
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
        _update_camael_gitops_status(incident_key, "FAILED", "UNHEALTHY_ROLLBACK")
        # Marcar RFC como fallido → Review
        if rfc_info:
            _close_rfc_async(rfc_info, deployment_name, namespace,
                             success=False, reason=f"Auto-rollback: {rollback_result}")
        if notify_fn:
            notify_fn(
                f"⚠️ AUTO-ROLLBACK en {namespace}/{deployment_name}: {rollback_result}",
                "HIGH",
            )
    else:
        logger.warning(f"[healer] {deployment_name} sigue unhealthy pero sin deploy reciente.")
        update_incident_fn(incident_key, "verify:unresolved")
        _update_camael_gitops_status(incident_key, "FAILED", "UNHEALTHY_NO_DEPLOY")
        if rfc_info:
            _close_rfc_async(rfc_info, deployment_name, namespace,
                             success=False, reason="Deployment sigue unhealthy. Intervención manual requerida.")
        if notify_fn:
            notify_fn(
                f"🚨 {namespace}/{deployment_name} SIGUE UNHEALTHY tras ROLLOUT_RESTART. "
                "Intervención manual requerida.",
                "CRITICAL",
            )


# ── Scheduler singleton (set from agent.py after BackgroundScheduler is created) ──
_aps_scheduler = None


def set_aps_scheduler(scheduler) -> None:
    """Almacena el BackgroundScheduler para que schedule_verification pueda agregar jobs."""
    global _aps_scheduler
    _aps_scheduler = scheduler


def schedule_verification(
    incident_key: str,
    deployment_name: str,
    namespace: str,
    update_incident_fn,
    notify_fn,
    generate_postmortem_fn=None,
    delay_seconds: int | None = None,
) -> None:
    """
    Schedula el job de verificación post-acción con APScheduler (P3-A).
    Se ejecuta delay_seconds después de la acción (default: SRE_VERIFICATION_DELAY env, 300s).
    """
    if delay_seconds is None:
        delay_seconds = _VERIFICATION_DELAY_S
    try:
        from datetime import datetime, timedelta
        scheduler = _aps_scheduler
        if scheduler is None:
            logger.warning("[healer] APScheduler no disponible — verificación no schedulada")
            return
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


# ── GitOps Handoff — Raphael → Camael ────────────────────────────────────────

#: Tipos de anomalía que tienen fix automático via GitOps (debe coincidir con BUG_LIBRARY).
_GITOPS_FIXABLE = {
    "OOM_KILLED", "CRASH_LOOP", "DEPLOYMENT_DEGRADED",
    "HIGH_MEMORY", "HIGH_CPU", "HIGH_RESTARTS", "MEMORY_LEAK_PREDICTED",
    "POD_FAILED",   # Fix via LLM — requiere análisis de logs para decidir estrategia
    # Note: all _GITOPS_FIXABLE types should also be in _AUTO_HEALABLE_ISSUES (see top of file)
}


def handoff_to_camael(
    anomaly: Anomaly,
    incident_key: str,
    notify_fn,
) -> None:
    """
    Inicia un GitOps fix asíncrono via Camael después de aplicar el fix temporal.

    Solo se activa si el issue_type tiene una entrada en BUG_LIBRARY.
    Se ejecuta en un daemon thread para no bloquear el SRE loop.

    Flujo:
      1. Camael lee el YAML de Bitbucket
      2. Aplica el patch (memoria, CPU, probe delay...)
      3. Crea branch + commit + PR
      4. Notifica por WhatsApp para aprobación humana
      5. Tras APROBAR: merge → ArgoCD despliega

    Args:
        anomaly:      La anomalía detectada por Raphael.
        incident_key: Clave única del incidente (para Redis y PR description).
        notify_fn:    Función de notificación WhatsApp del SRE loop.
    """
    if anomaly.issue_type not in _GITOPS_FIXABLE:
        return

    from agents.sre.bug_library import get_fix
    resource_name = anomaly.owner_name or anomaly.resource_name or ""
    fix = get_fix(anomaly.issue_type, resource_name)
    if not fix:
        return

    # Dedup: un solo PR por deployment (no por issue_type) — TTL 2h.
    # resource_name puede ser pod con hash (amael-demo-oom-7cf4c6c4b4-5zn6x),
    # derivamos el deployment base matcheando contra APP_MANIFEST_MAP.
    from agents.sre.bug_library import APP_MANIFEST_MAP
    from storage.redis.client import get_redis_client
    _deploy_base = resource_name
    for key in APP_MANIFEST_MAP:
        if resource_name == key or resource_name.startswith(key + "-"):
            _deploy_base = key
            break
    _gitops_dedup_key = f"sre:gitops:{anomaly.namespace}:{_deploy_base}"
    try:
        _redis = get_redis_client()
        if _redis.exists(_gitops_dedup_key):
            logger.debug(
                f"[healer] GitOps handoff ya en curso para {_deploy_base} — omitido "
                f"({anomaly.issue_type})"
            )
            return
        _redis.setex(_gitops_dedup_key, 7200, "1")  # TTL 2h
    except Exception:
        pass  # Si Redis falla, permitir el handoff de todas formas

    from observability.metrics import GITOPS_HANDOFF_TOTAL
    GITOPS_HANDOFF_TOTAL.labels(issue_type=anomaly.issue_type).inc()
    logger.info(
        f"[healer] Iniciando GitOps handoff → Camael "
        f"(issue={anomaly.issue_type}, resource={resource_name}, "
        f"repo={fix.repo}, file={fix.file_path}, incident={incident_key})"
    )
    threading.Thread(
        target=_run_gitops_fix_in_thread,
        args=(anomaly, incident_key, fix.repo),
        daemon=True,
        name=f"gitops-fix-{incident_key[:8]}",
    ).start()


def _run_gitops_fix_in_thread(anomaly: Anomaly, incident_key: str, repo: str) -> None:
    """
    Ejecuta el gitops_fix de Camael en un thread separado con su propio event loop.
    Necesario porque el SRE loop corre en un thread de APScheduler (no asyncio).
    """
    import asyncio
    import uuid

    from agents.base.agent_registry import AgentRegistry
    from agents.base.llm_factory import get_chat_llm
    from core.agent_base import AgentContext

    resource_name = anomaly.owner_name or anomaly.resource_name or ""
    _namespace    = anomaly.namespace or _DEFAULT_NAMESPACE

    # Recopilar contexto rico para el LLM — fallos silenciosos para no bloquear
    pod_logs = _get_pod_logs(resource_name, _namespace, lines=50)

    task = {
        "task":                    "gitops_fix",
        "incident_key":            incident_key,
        "issue_type":              anomaly.issue_type,
        "resource_name":           resource_name,
        "namespace":               _namespace,
        "details":                 anomaly.details[:400] if anomaly.details else "",
        "repo":                    repo,
        "user_id":                 "raphael-sre",
        # Contexto rico para razonamiento LLM
        "pod_logs":                pod_logs,
        "restart_count":           getattr(anomaly, "restart_count", 0),
        "current_memory_usage_mi": getattr(anomaly, "memory_usage_mi", None),
        "current_cpu_usage_m":     getattr(anomaly, "cpu_usage_m", None),
        "confidence":              getattr(anomaly, "confidence", 0.0),
        "detected_at":             getattr(anomaly, "detected_at", ""),
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        llm = get_chat_llm()
        ctx = AgentContext(
            user_id="raphael-sre",
            conversation_id=incident_key,
            request_id=str(uuid.uuid4()),
            llm=llm,
        )
        camael = AgentRegistry.get("camael", ctx)
        result = loop.run_until_complete(camael.execute(task))
        if result.success:
            logger.info(
                f"[healer] GitOps fix iniciado por Camael — "
                f"incident={incident_key} PR={result.output.get('pr_id', '?')}"
            )
        else:
            logger.error(
                f"[healer] GitOps fix falló — incident={incident_key} "
                f"error={result.error}"
            )
    except Exception as exc:
        logger.error(f"[healer] _run_gitops_fix_in_thread error: {exc}")
    finally:
        loop.close()


# ── ServiceNow RFC helpers ────────────────────────────────────────────────────

def _get_rfc_from_redis(incident_key: str) -> dict | None:
    """Lee el RFC info guardado por Camael en Redis (sn:rfc:{incident_key})."""
    try:
        import json as _json

        from storage.redis.client import get_client
        raw = get_client().get(f"sn:rfc:{incident_key}")
        return _json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug(f"[healer] No se pudo leer RFC de Redis: {exc}")
        return None


def _update_camael_gitops_status(incident_key: str, status: str, verification_result: str) -> None:
    """Actualiza el status de camael_gitops_actions en PostgreSQL. Best-effort."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE camael_gitops_actions
                    SET status = %s,
                        verification_result = %s,
                        updated_at = NOW()
                    WHERE incident_key = %s
                """, (status, verification_result, incident_key))
    except Exception as exc:
        logger.debug(f"[healer] _update_camael_gitops_status error: {exc}")


def _close_rfc_async(
    rfc_info: dict,
    deployment_name: str,
    namespace: str,
    success: bool,
    reason: str = "",
) -> None:
    """Cierra o falla el RFC en ServiceNow en un thread separado (no bloquea el verificador)."""
    def _do_close() -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                _update_rfc_state(rfc_info, deployment_name, namespace, success, reason)
            )
        except Exception as exc:
            logger.warning(f"[healer] _close_rfc_async error: {exc}")
        finally:
            loop.close()

    threading.Thread(target=_do_close, daemon=True).start()


async def _update_rfc_state(
    rfc_info: dict,
    deployment_name: str,
    namespace: str,
    success: bool,
    reason: str,
) -> None:
    """Actualiza el RFC en ServiceNow según el resultado de la verificación."""
    try:
        from agents.devops import servicenow_client as sn
        if not sn.is_configured():
            return

        sys_id = rfc_info.get("sys_id", "")
        number = rfc_info.get("number", "N/A")
        if not sys_id:
            return

        if success:
            await sn.close_rfc(
                sys_id,
                f"Despliegue verificado como exitoso por Raphael (SRE).\n"
                f"Deployment {namespace}/{deployment_name} saludable 5 min post-deploy.\n"
                f"RFC {number} cerrado automáticamente.",
            )
            logger.info(f"[healer] RFC {number} → Closed (verificación exitosa)")
        else:
            await sn.fail_rfc(
                sys_id,
                f"Verificación post-deploy fallida para {namespace}/{deployment_name}.\n"
                f"Razón: {reason}\n"
                f"RFC {number} requiere revisión manual.",
            )
            logger.warning(f"[healer] RFC {number} → Review (verificación fallida)")
    except Exception as exc:
        logger.warning(f"[healer] _update_rfc_state error: {exc}")
