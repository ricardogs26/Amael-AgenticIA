"""
KubernetesSkill — lectura del estado del clúster via Kubernetes API.

Capacidades:
  list_pods(namespace)           — pods con estado, reinicios y condición
  describe_pod(name, namespace)  — detalle + events del pod
  list_nodes()                   — nodos con condición y capacidad
  get_deployment(name, namespace)— estado del deployment
  get_events(namespace)          — últimos N events del namespace

Autenticación: incluster config → kubeconfig fallback (mismo patrón que healer.py).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.kubernetes")

_DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "amael-ia")

# ── Inputs ────────────────────────────────────────────────────────────────────

class ListPodsInput(SkillInput):
    namespace: str = _DEFAULT_NAMESPACE
    label_selector: str = ""

class DescribePodInput(SkillInput):
    pod_name: str
    namespace: str = _DEFAULT_NAMESPACE

class ListNodesInput(SkillInput):
    pass

class GetDeploymentInput(SkillInput):
    deployment_name: str
    namespace: str = _DEFAULT_NAMESPACE

class GetEventsInput(SkillInput):
    namespace: str = _DEFAULT_NAMESPACE
    limit: int = 20
    field_selector: str = ""


# ── K8s client factory ────────────────────────────────────────────────────────

def _get_core_v1():
    from kubernetes import client
    from kubernetes import config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return client.CoreV1Api()

def _get_apps_v1():
    from kubernetes import client
    from kubernetes import config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return client.AppsV1Api()


# ── Skill ─────────────────────────────────────────────────────────────────────

class KubernetesSkill(BaseSkill):
    """
    Capacidad de lectura del clúster Kubernetes.
    Usada por SREAgent y el agente conversacional para observar el estado del sistema.
    """

    name        = "kubernetes"
    description = "Lectura del estado del clúster K8s: pods, nodos, deployments, eventos"
    version     = "1.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        """Dispatcher basado en el tipo de input."""
        if isinstance(input, ListPodsInput):
            return await self.list_pods(input)
        if isinstance(input, DescribePodInput):
            return await self.describe_pod(input)
        if isinstance(input, ListNodesInput):
            return await self.list_nodes(input)
        if isinstance(input, GetDeploymentInput):
            return await self.get_deployment(input)
        if isinstance(input, GetEventsInput):
            return await self.get_events(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por KubernetesSkill")

    async def list_pods(self, input: ListPodsInput) -> SkillOutput:
        """Lista pods de un namespace con estado, phase y conteo de reinicios."""
        try:
            v1    = _get_core_v1()
            kwargs: dict[str, Any] = {"namespace": input.namespace}
            if input.label_selector:
                kwargs["label_selector"] = input.label_selector

            pods = v1.list_namespaced_pod(**kwargs)
            result = []
            for p in pods.items:
                restarts = sum(
                    cs.restart_count
                    for cs in (p.status.container_statuses or [])
                )
                containers = []
                for cs in (p.status.container_statuses or []):
                    state = cs.state
                    if state.waiting:
                        cstate = f"WAITING({state.waiting.reason})"
                    elif state.terminated:
                        cstate = f"TERMINATED({state.terminated.reason})"
                    else:
                        cstate = "RUNNING"
                    containers.append({
                        "name":     cs.name,
                        "state":    cstate,
                        "restarts": cs.restart_count,
                        "ready":    cs.ready,
                    })
                result.append({
                    "name":       p.metadata.name,
                    "namespace":  p.metadata.namespace,
                    "phase":      p.status.phase or "Unknown",
                    "restarts":   restarts,
                    "node":       p.spec.node_name or "",
                    "containers": containers,
                    "labels":     p.metadata.labels or {},
                })
            logger.debug(f"[k8s_skill] {len(result)} pods en {input.namespace}")
            return SkillOutput.ok(result, count=len(result), namespace=input.namespace)
        except Exception as exc:
            logger.error(f"[k8s_skill] list_pods error: {exc}")
            return SkillOutput.fail(str(exc))

    async def describe_pod(self, input: DescribePodInput) -> SkillOutput:
        """Describe un pod y sus últimos eventos — punto de partida para diagnóstico."""
        try:
            v1  = _get_core_v1()
            pod = v1.read_namespaced_pod(
                name=input.pod_name, namespace=input.namespace
            )
            evs = v1.list_namespaced_event(
                namespace=input.namespace,
                field_selector=f"involvedObject.name={input.pod_name}",
            )
            containers = []
            for cs in (pod.status.container_statuses or []):
                state = cs.state
                if state.waiting:
                    cstate = {"type": "WAITING", "reason": state.waiting.reason,
                              "message": state.waiting.message}
                elif state.terminated:
                    cstate = {"type": "TERMINATED", "reason": state.terminated.reason,
                              "exit_code": state.terminated.exit_code}
                else:
                    cstate = {"type": "RUNNING"}
                containers.append({
                    "name":     cs.name,
                    "state":    cstate,
                    "restarts": cs.restart_count,
                    "image":    cs.image,
                    "ready":    cs.ready,
                })
            events = [
                {
                    "type":    e.type,
                    "reason":  e.reason,
                    "message": e.message,
                    "count":   e.count,
                    "time":    str(e.last_timestamp),
                }
                for e in (evs.items or [])[-10:]
            ]
            result = {
                "name":       pod.metadata.name,
                "namespace":  pod.metadata.namespace,
                "phase":      pod.status.phase,
                "node":       pod.spec.node_name,
                "containers": containers,
                "events":     events,
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason}
                    for c in (pod.status.conditions or [])
                ],
            }
            return SkillOutput.ok(result)
        except Exception as exc:
            logger.error(f"[k8s_skill] describe_pod error: {exc}")
            return SkillOutput.fail(str(exc))

    async def list_nodes(self, input: ListNodesInput) -> SkillOutput:
        """Lista todos los nodos del clúster con su estado y capacidad."""
        try:
            v1    = _get_core_v1()
            nodes = v1.list_node()
            result = []
            for n in nodes.items:
                conditions = {
                    c.type: c.status
                    for c in (n.status.conditions or [])
                }
                capacity  = n.status.capacity or {}
                allocatable = n.status.allocatable or {}
                result.append({
                    "name":        n.metadata.name,
                    "ready":       conditions.get("Ready", "Unknown"),
                    "cpu":         capacity.get("cpu", "?"),
                    "memory":      capacity.get("memory", "?"),
                    "cpu_alloc":   allocatable.get("cpu", "?"),
                    "mem_alloc":   allocatable.get("memory", "?"),
                    "roles":       [
                        k.replace("node-role.kubernetes.io/", "")
                        for k in (n.metadata.labels or {})
                        if k.startswith("node-role.kubernetes.io/")
                    ],
                    "version":     n.status.node_info.kubelet_version if n.status.node_info else "",
                })
            return SkillOutput.ok(result, count=len(result))
        except Exception as exc:
            logger.error(f"[k8s_skill] list_nodes error: {exc}")
            return SkillOutput.fail(str(exc))

    async def get_deployment(self, input: GetDeploymentInput) -> SkillOutput:
        """Retorna el estado del deployment: réplicas deseadas/disponibles."""
        try:
            apps = _get_apps_v1()
            dep  = apps.read_namespaced_deployment(
                name=input.deployment_name, namespace=input.namespace
            )
            result = {
                "name":              dep.metadata.name,
                "namespace":         dep.metadata.namespace,
                "desired":           dep.spec.replicas or 0,
                "ready":             dep.status.ready_replicas or 0,
                "available":         dep.status.available_replicas or 0,
                "updated":           dep.status.updated_replicas or 0,
                "image":             dep.spec.template.spec.containers[0].image
                                     if dep.spec.template.spec.containers else "",
                "strategy":          dep.spec.strategy.type if dep.spec.strategy else "",
                "conditions":        [
                    {"type": c.type, "status": c.status, "reason": c.reason}
                    for c in (dep.status.conditions or [])
                ],
            }
            return SkillOutput.ok(result)
        except Exception as exc:
            logger.error(f"[k8s_skill] get_deployment error: {exc}")
            return SkillOutput.fail(str(exc))

    async def get_events(self, input: GetEventsInput) -> SkillOutput:
        """Retorna los últimos N eventos del namespace (Warning primero)."""
        try:
            v1  = _get_core_v1()
            kwargs: dict[str, Any] = {"namespace": input.namespace}
            if input.field_selector:
                kwargs["field_selector"] = input.field_selector

            evs = v1.list_namespaced_event(**kwargs)
            events = sorted(
                evs.items or [],
                key=lambda e: (e.type == "Normal", e.last_timestamp or ""),
            )
            result = [
                {
                    "type":      e.type,
                    "reason":    e.reason,
                    "object":    f"{e.involved_object.kind}/{e.involved_object.name}",
                    "message":   e.message,
                    "count":     e.count,
                    "timestamp": str(e.last_timestamp),
                }
                for e in events[-input.limit:]
            ]
            return SkillOutput.ok(result, count=len(result))
        except Exception as exc:
            logger.error(f"[k8s_skill] get_events error: {exc}")
            return SkillOutput.fail(str(exc))

    async def health_check(self) -> bool:
        """Verifica que la API de Kubernetes responde."""
        try:
            from kubernetes import client
            from kubernetes import config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            client.CoreV1Api().list_namespace(limit=1)
            return True
        except Exception as exc:
            logger.warning(f"[k8s_skill] health_check falló: {exc}")
            return False
