"""
SRE Observer — observa el estado del clúster desde múltiples fuentes.

Extrae las funciones de observación de k8s-agent/main.py:
  observe_cluster()  — estado estructural de pods y nodos (P0)
  observe_metrics()  — métricas CPU/memoria/errores via Prometheus (P4-A)
  observe_trends()   — predicción lineal + derivadas (P5-A)
  observe_slo()      — burn rate de error budget (P5-C)

Principio: Solo observa, nunca actúa. Retorna listas de Anomaly.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC

from agents.sre.models import Anomaly
from core.constants import AnomalyType, Severity

logger = logging.getLogger("agents.sre.observer")

# Namespaces a observar
_OBSERVE_NAMESPACES = [
    ns.strip()
    for ns in os.environ.get(
        "SRE_OBSERVE_NAMESPACES", "amael-ia,vault,observability,kong"
    ).split(",")
    if ns.strip()
]


def _get_k8s_client():
    """Inicializa el cliente Kubernetes (in-cluster o local)."""
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client


def _prometheus_query(url: str, query: str) -> list[dict] | None:
    """Ejecuta una query instant en Prometheus. Retorna los results o None."""
    try:
        import requests as _req
        resp = _req.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["result"]
    except Exception as exc:
        logger.warning(f"[observer] Prometheus query falló ({query[:60]}): {exc}")
    return None


def observe_cluster(namespaces: list[str] | None = None) -> list[Anomaly]:
    """
    Observa el estado estructural de pods y nodos vía Kubernetes API.

    Detecta:
      CRASH_LOOP, OOM_KILLED, IMAGE_PULL_ERROR, POD_FAILED,
      POD_PENDING_STUCK, HIGH_RESTARTS, NODE_NOT_READY

    Migrado desde k8s-agent/main.py → observe_cluster()
    """
    anomalies: list[Anomaly] = []
    ns_list = namespaces or _OBSERVE_NAMESPACES

    try:
        k8s = _get_k8s_client()
        v1      = k8s.CoreV1Api()
        _ = k8s.AppsV1Api()  # instanciado para warm-up del client; pods via v1

        # ── Pods ──────────────────────────────────────────────────────────────
        for ns in ns_list:
            try:
                pods = v1.list_namespaced_pod(namespace=ns)
            except Exception as exc:
                logger.warning(f"[observer] Error listando pods en {ns}: {exc}")
                continue

            for pod in pods.items:
                pod_name = pod.metadata.name
                phase = pod.status.phase or "Unknown"
                container_statuses = pod.status.container_statuses or []

                # Calcular owner (Deployment/StatefulSet)
                owner_name = ""
                for ref in (pod.metadata.owner_references or []):
                    if ref.kind in ("ReplicaSet", "StatefulSet", "DaemonSet"):
                        owner_name = ref.name.rsplit("-", 1)[0] if ref.kind == "ReplicaSet" else ref.name
                        break

                for cs in container_statuses:
                    restarts = cs.restart_count or 0
                    state = cs.state

                    # CRASH_LOOP
                    if state and state.waiting and state.waiting.reason in (
                        "CrashLoopBackOff", "Error"
                    ):
                        anomalies.append(Anomaly(
                            issue_type=AnomalyType.CRASH_LOOP,
                            severity=Severity.HIGH,
                            namespace=ns,
                            resource_name=pod_name,
                            resource_type="Pod",
                            owner_name=owner_name,
                            details=(
                                f"Pod {pod_name} en CrashLoopBackOff. "
                                f"Reinicios: {restarts}. "
                                f"Razón: {state.waiting.reason}"
                            ),
                        ))

                    # OOM_KILLED
                    elif (cs.last_state and cs.last_state.terminated
                          and cs.last_state.terminated.reason == "OOMKilled"):
                        anomalies.append(Anomaly(
                            issue_type=AnomalyType.OOM_KILLED,
                            severity=Severity.HIGH,
                            namespace=ns,
                            resource_name=pod_name,
                            resource_type="Pod",
                            owner_name=owner_name,
                            details=f"Pod {pod_name} terminado por OOMKilled. Reinicios: {restarts}",
                        ))

                    # IMAGE_PULL_ERROR
                    elif state and state.waiting and state.waiting.reason in (
                        "ImagePullBackOff", "ErrImagePull"
                    ):
                        anomalies.append(Anomaly(
                            issue_type=AnomalyType.IMAGE_PULL_ERROR,
                            severity=Severity.MEDIUM,
                            namespace=ns,
                            resource_name=pod_name,
                            resource_type="Pod",
                            owner_name=owner_name,
                            details=f"Pod {pod_name} no puede descargar la imagen: {state.waiting.reason}",
                        ))

                    # HIGH_RESTARTS
                    elif restarts >= 5 and phase == "Running":
                        anomalies.append(Anomaly(
                            issue_type=AnomalyType.HIGH_RESTARTS,
                            severity=Severity.MEDIUM if restarts < 10 else Severity.HIGH,
                            namespace=ns,
                            resource_name=pod_name,
                            resource_type="Pod",
                            owner_name=owner_name,
                            details=f"Pod {pod_name} tiene {restarts} reinicios.",
                        ))

                # POD_FAILED
                if phase == "Failed":
                    anomalies.append(Anomaly(
                        issue_type=AnomalyType.POD_FAILED,
                        severity=Severity.HIGH,
                        namespace=ns,
                        resource_name=pod_name,
                        resource_type="Pod",
                        owner_name=owner_name,
                        details=f"Pod {pod_name} en estado Failed.",
                    ))

                # POD_PENDING_STUCK (pending > 5 min sin scheduling)
                elif phase == "Pending":
                    import time as _time
                    creation = pod.metadata.creation_timestamp
                    if creation:
                        age = (_time.time() - creation.replace(tzinfo=UTC).timestamp())
                        if age > 300:
                            anomalies.append(Anomaly(
                                issue_type=AnomalyType.POD_PENDING_STUCK,
                                severity=Severity.MEDIUM,
                                namespace=ns,
                                resource_name=pod_name,
                                resource_type="Pod",
                                owner_name=owner_name,
                                details=f"Pod {pod_name} en Pending por {int(age/60)} minutos.",
                            ))

        # ── Nodos ──────────────────────────────────────────────────────────────
        try:
            nodes = v1.list_node()
            for node in nodes.items:
                node_name = node.metadata.name
                for condition in (node.status.conditions or []):
                    if condition.type == "Ready" and condition.status != "True":
                        anomalies.append(Anomaly(
                            issue_type=AnomalyType.NODE_NOT_READY,
                            severity=Severity.CRITICAL,
                            namespace="cluster",
                            resource_name=node_name,
                            resource_type="Node",
                            details=(
                                f"Nodo {node_name} no está Ready. "
                                f"Razón: {condition.reason}. "
                                f"Mensaje: {condition.message}"
                            ),
                        ))
        except Exception as exc:
            logger.warning(f"[observer] Error listando nodos: {exc}")

    except Exception as exc:
        logger.error(f"[observer] Error en observe_cluster: {exc}", exc_info=True)

    if anomalies:
        logger.info(f"[observer] observe_cluster: {len(anomalies)} anomalías detectadas.")
    else:
        logger.debug("[observer] observe_cluster: clúster saludable.")

    return anomalies


def observe_metrics(prometheus_url: str) -> list[Anomaly]:
    """
    Observa métricas de CPU, memoria y tasa de errores vía Prometheus (P4-A).

    Detecta: HIGH_CPU, HIGH_MEMORY, HIGH_ERROR_RATE

    Migrado desde k8s-agent/main.py → observe_metrics()
    """
    cpu_threshold    = float(os.environ.get("SRE_CPU_THRESHOLD",    "0.85"))
    memory_threshold = float(os.environ.get("SRE_MEMORY_THRESHOLD", "0.85"))
    anomalies: list[Anomaly] = []

    # CPU
    cpu_results = _prometheus_query(
        prometheus_url,
        'sum(rate(container_cpu_usage_seconds_total{namespace=~"amael-ia|vault|observability",'
        'container!=""}[5m])) by (namespace, pod) / '
        'sum(kube_pod_container_resource_limits{resource="cpu",'
        'namespace=~"amael-ia|vault|observability"}) by (namespace, pod)',
    )
    for r in (cpu_results or []):
        try:
            ratio = float(r["value"][1])
            if ratio > cpu_threshold:
                labels = r.get("metric", {})
                ns  = labels.get("namespace", "unknown")
                pod = labels.get("pod", "unknown")
                anomalies.append(Anomaly(
                    issue_type=AnomalyType.HIGH_CPU,
                    severity=Severity.HIGH if ratio > 0.95 else Severity.MEDIUM,
                    namespace=ns,
                    resource_name=pod,
                    resource_type="Pod",
                    details=f"CPU usage {ratio:.1%} supera umbral {cpu_threshold:.0%}",
                ))
        except (ValueError, KeyError):
            pass

    # Memoria
    mem_results = _prometheus_query(
        prometheus_url,
        'sum(container_memory_working_set_bytes{namespace=~"amael-ia|vault",'
        'container!=""}) by (namespace, pod) / '
        'sum(kube_pod_container_resource_limits{resource="memory",'
        'namespace=~"amael-ia|vault"}) by (namespace, pod)',
    )
    for r in (mem_results or []):
        try:
            ratio = float(r["value"][1])
            if ratio > memory_threshold:
                labels = r.get("metric", {})
                ns  = labels.get("namespace", "unknown")
                pod = labels.get("pod", "unknown")
                anomalies.append(Anomaly(
                    issue_type=AnomalyType.HIGH_MEMORY,
                    severity=Severity.HIGH if ratio > 0.95 else Severity.MEDIUM,
                    namespace=ns,
                    resource_name=pod,
                    resource_type="Pod",
                    details=f"Memoria {ratio:.1%} supera umbral {memory_threshold:.0%}",
                ))
        except (ValueError, KeyError):
            pass

    # Tasa de errores 5xx
    error_results = _prometheus_query(
        prometheus_url,
        'sum(rate(http_requests_total{namespace="amael-ia",status=~"5.."}[5m])) by (handler) / '
        'sum(rate(http_requests_total{namespace="amael-ia"}[5m])) by (handler)',
    )
    for r in (error_results or []):
        try:
            ratio = float(r["value"][1])
            if ratio > 0.05:
                handler = r.get("metric", {}).get("handler", "unknown")
                anomalies.append(Anomaly(
                    issue_type=AnomalyType.HIGH_ERROR_RATE,
                    severity=Severity.HIGH if ratio > 0.10 else Severity.MEDIUM,
                    namespace="amael-ia",
                    resource_name=handler,
                    resource_type="Endpoint",
                    details=f"Error rate {ratio:.1%} en handler '{handler}'",
                ))
        except (ValueError, KeyError):
            pass

    if anomalies:
        logger.info(f"[observer] observe_metrics: {len(anomalies)} anomalías de métricas.")
    return anomalies


def observe_trends(prometheus_url: str) -> list[Anomaly]:
    """
    Detección predictiva via predict_linear() y deriv() en Prometheus (P5-A).

    Detecta:
      DISK_EXHAUSTION_PREDICTED  — predicción de disco lleno
      MEMORY_LEAK_PREDICTED      — crecimiento anormal de memoria
      ERROR_RATE_ESCALATING      — tasa de errores en aumento

    Migrado desde k8s-agent/main.py → observe_trends()
    """
    anomalies: list[Anomaly] = []
    leak_rate = int(os.environ.get("SRE_MEMORY_LEAK_RATE_BYTES", str(1024 * 1024)))

    # Predicción de disco lleno en 4 horas
    disk_results = _prometheus_query(
        prometheus_url,
        "predict_linear(node_filesystem_avail_bytes"
        '{mountpoint="/",fstype!="tmpfs"}[1h], 4*3600) < 0',
    )
    for r in (disk_results or []):
        node = r.get("metric", {}).get("instance", "unknown")
        anomalies.append(Anomaly(
            issue_type=AnomalyType.DISK_EXHAUSTION_PREDICTED,
            severity=Severity.HIGH,
            namespace="cluster",
            resource_name=node,
            resource_type="Node",
            details=f"Disco del nodo {node} se agotará en < 4 horas según predict_linear.",
        ))

    # Memory leak (derivada de memoria > umbral por container)
    mem_deriv_results = _prometheus_query(
        prometheus_url,
        f"deriv(container_memory_working_set_bytes"
        f'{{namespace="amael-ia",container!=""}}[30m]) > {leak_rate}',
    )
    for r in (mem_deriv_results or []):
        labels = r.get("metric", {})
        pod = labels.get("pod", "unknown")
        ns  = labels.get("namespace", "amael-ia")
        try:
            rate_bytes = float(r["value"][1])
            anomalies.append(Anomaly(
                issue_type=AnomalyType.MEMORY_LEAK_PREDICTED,
                severity=Severity.MEDIUM,
                namespace=ns,
                resource_name=pod,
                resource_type="Pod",
                details=(
                    f"Posible memory leak en {pod}: "
                    f"crecimiento {rate_bytes/1024:.1f} KB/s en últimos 30min."
                ),
            ))
        except (ValueError, KeyError):
            pass

    # Error rate escalating
    error_deriv_results = _prometheus_query(
        prometheus_url,
        'deriv(rate(http_requests_total{namespace="amael-ia",status=~"5.."}[5m])[15m:]) > 0.01',
    )
    for r in (error_deriv_results or []):
        handler = r.get("metric", {}).get("handler", "unknown")
        anomalies.append(Anomaly(
            issue_type=AnomalyType.ERROR_RATE_ESCALATING,
            severity=Severity.MEDIUM,
            namespace="amael-ia",
            resource_name=handler,
            resource_type="Endpoint",
            details=f"Tasa de errores en '{handler}' está aumentando consistentemente.",
        ))

    if anomalies:
        logger.info(f"[observer] observe_trends: {len(anomalies)} tendencias anómalas.")
    return anomalies


def observe_slo(prometheus_url: str, slo_targets: list[dict]) -> list[Anomaly]:
    """
    Verifica burn rate del error budget para cada SLO definido (P5-C).

    Detecta: SLO_BUDGET_BURNING

    Migrado desde k8s-agent/main.py → observe_slo()
    """
    from observability.metrics import SRE_SLO_VIOLATIONS_TOTAL
    anomalies: list[Anomaly] = []

    for slo in slo_targets:
        handler = slo.get("handler", "")
        target  = float(slo.get("availability_target", 0.995))
        window  = slo.get("window_hours", 24)

        query = (
            f'1 - (sum(rate(http_requests_total{{namespace="amael-ia",'
            f'handler=~"{handler}",status=~"5.."}}[{window}h])) / '
            f'sum(rate(http_requests_total{{namespace="amael-ia",'
            f'handler=~"{handler}"}}[{window}h])))'
        )
        results = _prometheus_query(prometheus_url, query)
        if not results:
            continue

        try:
            availability = float(results[0]["value"][1])
            budget_remaining = (availability - target) / (1 - target)
            burn_rate = 1 - budget_remaining

            if burn_rate > 0.5:  # Quemando más del 50% del error budget
                SRE_SLO_VIOLATIONS_TOTAL.labels(service=handler).inc()
                anomalies.append(Anomaly(
                    issue_type=AnomalyType.SLO_BUDGET_BURNING,
                    severity=Severity.CRITICAL if burn_rate > 0.9 else Severity.HIGH,
                    namespace="amael-ia",
                    resource_name=handler,
                    resource_type="Service",
                    details=(
                        f"SLO '{handler}': disponibilidad {availability:.3%} "
                        f"(target {target:.3%}). "
                        f"Error budget quemado: {burn_rate:.0%}."
                    ),
                    metadata={
                        "availability": availability,
                        "target": target,
                        "burn_rate": burn_rate,
                    },
                ))
        except (ValueError, KeyError, ZeroDivisionError):
            pass

    if anomalies:
        logger.info(f"[observer] observe_slo: {len(anomalies)} SLOs en riesgo.")
    return anomalies
