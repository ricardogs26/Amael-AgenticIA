"""
SRE Commands — dispatcher de comandos WhatsApp `/sre <cmd>` (P5-E/P7/P8,
portado del legacy k8s-agent 5.2.0).

`handle_command()` recibe el comando en texto plano y retorna la respuesta
lista para WhatsApp. Lo consume el router `/api/sre/command` (inprocess en
raphael-service; el backend en modo remote lo proxy-ea vía HTTP).
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("agents.sre.commands")

_DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "amael-ia")

_ISSUE_LABELS = {
    "CRASH_LOOP":                "CrashLoopBackOff",
    "OOM_KILLED":                "OOMKilled (memoria agotada)",
    "HIGH_CPU":                  "CPU elevada",
    "HIGH_MEMORY":               "Memoria elevada",
    "HIGH_ERROR_RATE":           "Alta tasa de errores HTTP",
    "IMAGE_PULL_ERROR":          "Error al descargar imagen",
    "POD_FAILED":                "Pod en estado fallido",
    "POD_PENDING_STUCK":         "Pod bloqueado en Pending",
    "NODE_NOT_READY":            "Nodo no disponible",
    "MEMORY_LEAK_PREDICTED":     "Memory leak predicho (tendencia)",
    "DISK_EXHAUSTION_PREDICTED": "Agotamiento de disco predicho",
    "ERROR_RATE_ESCALATING":     "Tasa de errores escalando",
    "SLO_BUDGET_BURNING":        "SLO en riesgo (error budget)",
    "DEPLOYMENT_DEGRADED":       "Deployment degradado (réplicas < deseadas)",
}

_ACTION_LABELS = {
    "ROLLOUT_RESTART": "reinicio automático ejecutado",
    "NOTIFY_HUMAN":    "notificación enviada al equipo on-call (acción humana requerida)",
    "ROLLBACK":        "rollback automático ejecutado",
    "SCALE_UP":        "escalado solicitado (pendiente aprobación humana)",
    "PATCH_RESOURCES": "incremento de límite de memoria solicitado",
    "NO_ACTION":       "sin acción — modo OBSERVE activo",
    "OBSERVE_ONLY":    "sin acción — modo OBSERVE activo",
}

_GRAFANA_DASHBOARDS = [
    ("amael-llm",          "LLM & HTTP — latencia, tokens, throughput"),
    ("amael-agent",        "Pipeline de Agente — pasos, herramientas, REPLAN"),
    ("amael-rag",          "RAG Performance — hit/miss, latencia"),
    ("amael-infra",        "Infraestructura & GPU — VRAM, CPU, pods"),
    ("amael-supervisor",   "Supervisor & Calidad — quality scores"),
    ("amael-security",     "Seguridad & Rate Limiting"),
    ("amael-service-map",  "Service Map — topología OTel"),
    ("amael-sre-agent",    "SRE Autónomo — loop runs, anomalías, acciones"),
    ("amael-backend",      "Backend Overview — golden signals completos"),
    ("amael-poc-overview", "POC Overview"),
    ("amael-agent-comm",   "Comunicación entre Agentes"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_duration(duration_str: str) -> int:
    """Convierte '24h', '1h', '30m' o '90' a minutos. Default 60."""
    if not duration_str:
        return 60
    duration_str = duration_str.strip().lower()
    if duration_str.isdigit():
        return int(duration_str)
    match = re.match(r"(\d+)([hm])?", duration_str)
    if not match:
        return 60
    val, unit = match.groups()
    val = int(val)
    return val * 60 if unit == "h" else val


def _parse_alert_context(text: str | None) -> dict | None:
    """Extrae namespace, recurso e issue del texto de una alerta citada."""
    if not text:
        return None
    try:
        issue_match = re.search(r"(?:⚠️ [^:]+|Tipo): ([A-Z_]+)", text)
        res_match   = re.search(r"Recurso: (?:([^\s/(]+)/)?([^\s(]+)(?: \(([^)]+)\))?", text)
        if issue_match and res_match:
            ns = res_match.group(1) or res_match.group(3) or _DEFAULT_NAMESPACE
            return {
                "issue":     issue_match.group(1),
                "resource":  res_match.group(2),
                "namespace": ns,
            }
    except Exception:
        pass
    return None


def _ts(value) -> str:
    """Timestamp legible desde datetime o string."""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m %H:%M")
    return str(value or "?")[:16].replace("T", " ")


def _format_incident_narrative(row: dict) -> str:
    """Convierte una fila de sre_incidents en narrativa en español."""
    ts         = row.get("created_at")
    time_str   = ts.strftime("%d/%m/%Y %H:%M") if hasattr(ts, "strftime") else str(ts)
    root_cause = row.get("root_cause") or ""
    confidence = row.get("confidence")
    result     = row.get("action_result") or ""

    issue_label  = _ISSUE_LABELS.get(row.get("issue_type", ""), row.get("issue_type", "?"))
    action_label = _ACTION_LABELS.get(row.get("action_taken", ""), row.get("action_taken", ""))
    conf_str     = f"{confidence * 100:.0f}%" if confidence is not None else "N/A"
    result_icon  = (
        "✓ resuelto" if result and ("✅" in result or "healthy" in result.lower())
        else ("⚠ pendiente" if result else "")
    )

    lines = [
        f"[{time_str}] *{issue_label}* detectado en "
        f"`{row.get('resource_name', '?')}` (namespace: {row.get('namespace', '?')})"
    ]
    if root_cause:
        lines.append(f"  Diagnóstico: {root_cause[:200]} — confianza {conf_str}")
    lines.append(f"  Acción: {action_label}")
    if result_icon:
        lines.append(f"  Resultado: {result_icon}")
    return "\n".join(lines)


def _help_text() -> str:
    from agents.sre.autonomy import get_agent_mode
    return (
        "📋 *Comandos SRE disponibles:*\n"
        "• `estado` — estado de pods en todos los namespaces\n"
        "• `briefing` — morning briefing ahora (salud + SLO + predicciones)\n"
        "• `retro` — retrospectiva semanal ahora\n"
        "• `grafana` — listar dashboards de Grafana\n"
        "• `status` — estado del loop y circuit breaker\n"
        "• `incidents` — últimos 5 incidentes\n"
        "• `postmortems` — últimos 3 postmortems\n"
        "• `report` — resumen de las últimas 24h\n"
        "• `reporte` — audit log legible de decisiones del agente\n"
        "• `slo` — estado de SLOs\n"
        "• `modo` — ver modo de autonomía actual\n"
        "• `modo observe|conservative|standard|full` — cambiar modo\n"
        "• `pendiente` — ver aprobaciones pendientes\n"
        "• `si` — aprobar la acción pendiente\n"
        "• `no` — cancelar la acción pendiente\n"
        "• `rollout [deployment]` — ROLLOUT_RESTART (conservador)\n"
        "• `silent <dur>` — silenciar alerta citada (ej. 2h)\n"
        "• `maintenance on <min>` — activar mantenimiento\n"
        "• `maintenance off` — desactivar mantenimiento\n"
        f"\n🔧 Modo actual: *{get_agent_mode().upper()}*"
    )


# ── Handlers por comando ──────────────────────────────────────────────────────

def _cmd_estado() -> str:
    from kubernetes import client as k8s

    from agents.sre.observer import _OBSERVE_NAMESPACES, _get_k8s_client

    _get_k8s_client()
    v1 = k8s.CoreV1Api()
    lines = ["🖥️ *Estado del cluster Kubernetes*"]
    total_pods, problem_pods = 0, []
    for ns in _OBSERVE_NAMESPACES:
        try:
            ns_lines = []
            for pod in v1.list_namespaced_pod(ns).items:
                total_pods += 1
                phase = pod.status.phase or "Unknown"
                rc, wr = 0, ""
                for cs in (pod.status.container_statuses or []):
                    rc += cs.restart_count or 0
                    if cs.state and cs.state.waiting:
                        wr = cs.state.waiting.reason or ""
                status = wr if wr else phase
                icon = (
                    "✅" if status in ("Running", "Succeeded")
                    else ("⚠️" if status in ("Pending", "Terminating") else "❌")
                )
                entry = f"  {icon} {pod.metadata.name}: {status}" + (f" (↺{rc})" if rc > 0 else "")
                ns_lines.append(entry)
                if status not in ("Running", "Succeeded", "Terminating"):
                    problem_pods.append(f"{ns}/{pod.metadata.name}")
            if ns_lines:
                lines.append(f"\n*{ns}:*")
                lines.extend(ns_lines)
        except Exception as exc:
            lines.append(f"\n*{ns}:* ❌ Error: {exc}")
    lines.append(f"\n📊 Total pods: {total_pods}")
    if problem_pods:
        lines.append(f"🚨 Problemas detectados: {', '.join(problem_pods)}")
    else:
        lines.append("✅ Todos los pods saludables")
    return "\n".join(lines)


def _cmd_approve() -> str:
    from agents.sre import healer
    from agents.sre.approvals import get_pending_approval, remove_approval

    approval = get_pending_approval()
    if not approval:
        return "✅ Sin aprobaciones pendientes."
    action, resource, namespace = approval["action"], approval["resource"], approval["namespace"]
    remove_approval(approval["incident_key"])

    if action == "SCALE_UP":
        result = healer.scale_deployment_replicas(resource, namespace)
        return f"✅ *SCALE_UP aprobado y ejecutado:*\n{result}"
    if action == "PATCH_RESOURCES":
        result = healer.patch_deployment_memory_limit(resource, namespace)
        return f"✅ *PATCH_RESOURCES aprobado y ejecutado:*\n{result}"
    if action == "ROLLOUT_RESTART":
        result = healer.rollout_restart(resource, namespace)
        healer.record_restart(resource, namespace)
        return f"✅ *ROLLOUT_RESTART aprobado y ejecutado:*\n{result}"
    return f"✅ Acción {action} en `{resource}` aprobada (sin ejecutor automático)."


def _cmd_reject() -> str:
    from agents.sre.approvals import get_pending_approval, remove_approval

    approval = get_pending_approval()
    if not approval:
        return "Sin aprobaciones pendientes."
    remove_approval(approval["incident_key"])
    return (
        f"❌ *Acción cancelada:* {approval['action']} en "
        f"`{approval['resource']}` ({approval['namespace']})."
    )


def _cmd_rollout(cmd: str) -> str:
    from agents.sre import healer
    from agents.sre.approvals import get_pending_approval, remove_approval

    approval = get_pending_approval()
    if approval:
        resource, namespace = approval["resource"], approval["namespace"]
        remove_approval(approval["incident_key"])
        result = healer.rollout_restart(resource, namespace)
        if "✅" in result:
            healer.record_restart(resource, namespace)
        return f"🔄 *ROLLOUT_RESTART (conservador):*\n{result}"
    parts = cmd.split(None, 1)
    if len(parts) > 1:
        target = parts[1].strip()
        result = healer.rollout_restart(target, _DEFAULT_NAMESPACE)
        return f"🔄 {result}"
    return "Sin aprobación pendiente. Usa: `sre rollout <deployment>`"


def _cmd_pending() -> str:
    from agents.sre.approvals import list_pending_approvals

    approvals = list_pending_approvals()
    if not approvals:
        return "✅ Sin aprobaciones pendientes."
    lines = [f"⏳ *Aprobaciones pendientes ({len(approvals)}):*"]
    for ap in approvals:
        ts = ap.get("created_at", "")[:16].replace("T", " ")
        conf = f" ({ap['confidence']:.0%})" if ap.get("confidence") else ""
        lines.append(f"• [{ts}] *{ap['action']}* en `{ap['resource']}`{conf}")
        if ap.get("root_cause"):
            lines.append(f"  └ {ap['root_cause'][:70]}")
    lines.append("\n*Responde:* `sre si` | `sre no` | `sre rollout`")
    return "\n".join(lines)


def _cmd_status() -> str:
    from agents.sre.autonomy import get_agent_mode
    from agents.sre.observer import _OBSERVE_NAMESPACES
    from agents.sre.scheduler import get_loop_state

    state = get_loop_state()
    return (
        f"🤖 *SRE Agent (raphael) — Estado*\n"
        f"• Loop: {'✅ activo' if state.loop_enabled else '❌ inactivo'}\n"
        f"• Circuit Breaker: {state.circuit_breaker_state}\n"
        f"• Mantenimiento: {'⚙️ activo' if state.maintenance_active else '✅ inactivo'}\n"
        f"• Modo: {get_agent_mode().upper()}\n"
        f"• Última corrida: {state.last_run_result or '—'} "
        f"({state.anomalies_in_last_run} anomalías)\n"
        f"• Namespaces: {', '.join(_OBSERVE_NAMESPACES)}"
    )


def _cmd_incidents() -> str:
    from agents.sre.reporter import get_recent_incidents

    incidents = get_recent_incidents(limit=5)
    if not incidents:
        return "✅ Sin incidentes recientes."
    lines = ["📋 *Últimos incidentes:*"]
    for inc in incidents:
        lines.append(
            f"• [{_ts(inc.get('created_at'))}] {inc.get('issue_type', '?')} — "
            f"{inc.get('resource_name', '?')} ({inc.get('severity', '?')}) "
            f"→ {inc.get('action_taken', '?')}"
        )
    return "\n".join(lines)


def _cmd_postmortems() -> str:
    from agents.sre.reporter import get_recent_postmortems

    pms = get_recent_postmortems(limit=3)
    if not pms:
        return "📭 Sin postmortems generados aún."
    lines = ["📝 *Últimos postmortems:*"]
    for pm in pms:
        lines.append(
            f"• [{_ts(pm.get('created_at'))[:10]}] {pm.get('issue_type', '?')} — "
            f"{pm.get('resource_name', '?')}\n"
            f"  Causa: {(pm.get('root_cause_summary') or '')[:80]}"
        )
    return "\n".join(lines)


def _cmd_slo() -> str:
    import json as _json

    try:
        targets = _json.loads(os.environ.get("SLO_TARGETS_JSON", "[]"))
    except Exception:
        targets = []
    if not targets:
        return "📭 No hay SLO targets configurados (SLO_TARGETS_JSON vacío)."
    lines = ["📊 *SLO Targets:*"]
    for slo in targets:
        lines.append(
            f"• {slo.get('service', slo.get('handler', '?'))}: "
            f"target {slo.get('availability', 0):.1%}"
        )
    return "\n".join(lines)


def _cmd_maintenance_or_silent(cmd: str, quoted_text: str | None) -> str:
    from agents.sre.scheduler import activate_maintenance, deactivate_maintenance

    parts = cmd.split()
    if len(parts) >= 2 and parts[1] in ("off", "unsilent", "resume"):
        return deactivate_maintenance()

    duration_part = "60"
    if cmd.startswith("maintenance"):
        if len(parts) >= 3 and parts[1] == "on":
            duration_part = parts[2]
    elif len(parts) >= 2:  # silent <dur>
        duration_part = parts[1]

    # Silenciado específico si el usuario respondió citando una alerta
    if cmd.startswith("silent"):
        context = _parse_alert_context(quoted_text)
        if context:
            try:
                from storage.redis.client import get_client
                minutes = _parse_duration(duration_part)
                key = f"sre:silent:{context['namespace']}:{context['resource']}:{context['issue']}"
                get_client().set(key, "1", ex=minutes * 60)
                return (
                    f"🔇 Alerta silenciada: *{context['issue']}* en "
                    f"*{context['resource']}* ({context['namespace']}) por {minutes} min."
                )
            except Exception as exc:
                return f"❌ Error silenciando: {exc}"
        return (
            "Para silenciar una alerta específica, responde citando el mensaje "
            "de la alerta con `silent <duración>` (ej. `silent 2h`)."
        )

    minutes = max(1, min(_parse_duration(duration_part), 10080))
    return activate_maintenance(minutes)


def _cmd_grafana() -> str:
    lines = ["📊 *Dashboards de Grafana:*"]
    for uid, desc in _GRAFANA_DASHBOARDS:
        lines.append(f"• `{uid}` — {desc}")
    lines.append("\n🔗 grafana.richardx.dev")
    return "\n".join(lines)


def _cmd_audit() -> str:
    from agents.sre.reporter import get_recent_incidents

    rows = get_recent_incidents(limit=5)
    if not rows:
        return "📋 No hay incidentes registrados aún."
    lines = ["📋 *Últimas decisiones del agente SRE:*\n"]
    for row in rows:
        lines.append(_format_incident_narrative(row))
        lines.append("")
    auto = sum(
        1 for r in rows
        if r.get("action_taken") == "ROLLOUT_RESTART" and "✅" in (r.get("action_result") or "")
    )
    lines.append(f"✅ Auto-resueltos: {auto}/{len(rows)} en los últimos registros")
    return "\n".join(lines)


def _cmd_mode(cmd: str) -> str:
    from agents.sre.autonomy import (
        MODE_DESCRIPTIONS,
        VALID_MODES,
        get_agent_mode,
        set_agent_mode,
    )

    parts = cmd.split(maxsplit=1)
    if len(parts) == 1:
        mode = get_agent_mode()
        return (
            f"🔧 *Modo actual del agente SRE:*\n{MODE_DESCRIPTIONS.get(mode, mode)}\n\n"
            f"Modos disponibles: observe | conservative | standard | full"
        )
    new_mode = parts[1].strip().lower()
    if new_mode not in VALID_MODES:
        return (
            f"❌ Modo inválido '{new_mode}'.\n"
            f"Válidos: observe | conservative | standard | full"
        )
    if set_agent_mode(new_mode):
        return f"✅ *Modo cambiado a: {new_mode.upper()}*\n{MODE_DESCRIPTIONS[new_mode]}"
    return "❌ Error al cambiar el modo. Verifica Redis."


# ── Dispatcher principal ──────────────────────────────────────────────────────

def handle_command(command: str, quoted_text: str | None = None) -> str:
    """
    Procesa un comando `/sre <cmd>` y retorna la respuesta para WhatsApp.
    Nunca lanza — los errores se devuelven como texto.
    """
    cmd = (command or "").strip().lower()

    try:
        if not cmd or cmd == "ayuda":
            return _help_text()
        if cmd == "briefing":
            from agents.sre.briefing import generate_morning_briefing
            return generate_morning_briefing()
        if cmd in ("retro", "retrospectiva", "weekly"):
            from agents.sre.briefing import generate_weekly_retrospective
            return generate_weekly_retrospective()
        if cmd == "report":
            from agents.sre.briefing import generate_daily_summary
            return generate_daily_summary()
        if cmd in ("si", "sí", "yes", "approve", "aprobar"):
            return _cmd_approve()
        if cmd in ("no", "cancel", "cancelar"):
            return _cmd_reject()
        if cmd in ("rollout", "restart", "reiniciar") or cmd.startswith(
            ("rollout ", "restart ", "reiniciar ")
        ):
            return _cmd_rollout(cmd)
        if cmd in ("pendiente", "pending", "approvals"):
            return _cmd_pending()
        if cmd == "status":
            return _cmd_status()
        if cmd == "incidents":
            return _cmd_incidents()
        if cmd == "postmortems":
            return _cmd_postmortems()
        if cmd == "slo":
            return _cmd_slo()
        if cmd.startswith("maintenance") or cmd.startswith("silent"):
            return _cmd_maintenance_or_silent(cmd, quoted_text)
        if cmd == "estado":
            return _cmd_estado()
        if cmd == "grafana":
            return _cmd_grafana()
        if cmd in ("reporte", "audit", "log"):
            return _cmd_audit()
        if cmd == "modo" or cmd.startswith("modo "):
            return _cmd_mode(cmd)
        return f"❓ Comando desconocido: '{cmd}'. Escribe `ayuda` para ver opciones."
    except Exception as exc:
        logger.error(f"[commands] Error procesando '{cmd}': {exc}", exc_info=True)
        return f"❌ Error procesando comando '{cmd}': {exc}"
