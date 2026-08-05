"""
SRE Briefing — reportes proactivos por WhatsApp (P5/P7, portados del legacy
k8s-agent).

    generate_daily_summary()         — resumen 24h (cron 20:00 MX)
    generate_morning_briefing()      — SLO + salud + memoria + predicciones (cron 7:00 MX)
    generate_weekly_retrospective()  — retro semanal con recomendaciones LLM (lunes 8:00 MX)

Las funciones `send_*` envían el reporte por WhatsApp vía reporter.notify_whatsapp_sre.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger("agents.sre.briefing")

_SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}


def _prometheus_url() -> str:
    return os.environ.get(
        "PROMETHEUS_URL",
        "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090",
    )


def _slo_targets() -> list[dict]:
    try:
        return json.loads(os.environ.get("SLO_TARGETS_JSON", "[]"))
    except Exception:
        return []


# ── Reporte diario (P5) ───────────────────────────────────────────────────────

def generate_daily_summary() -> str:
    """Resumen de incidentes y acciones de las últimas 24 horas."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE action_taken = 'ROLLOUT_RESTART'
                                       AND action_result LIKE '%%✅%%') AS healed,
                    COUNT(*) FILTER (WHERE action_taken = 'NOTIFY_HUMAN') AS notified
                FROM sre_incidents
                WHERE created_at > now() - INTERVAL '24 hours';
            """)
            total, healed, notified = cur.fetchone() or (0, 0, 0)

            cur.execute("""
                SELECT
                    i.issue_type,
                    COUNT(*)        AS cnt,
                    MAX(i.severity) AS max_severity,
                    (SELECT details FROM sre_incidents i2
                      WHERE i2.issue_type = i.issue_type
                        AND i2.created_at > now() - INTERVAL '24 hours'
                      ORDER BY i2.created_at DESC LIMIT 1) AS last_detail,
                    (SELECT resource_name FROM sre_incidents i2
                      WHERE i2.issue_type = i.issue_type
                        AND i2.created_at > now() - INTERVAL '24 hours'
                      ORDER BY i2.created_at DESC LIMIT 1) AS last_resource
                FROM sre_incidents i
                WHERE i.created_at > now() - INTERVAL '24 hours'
                GROUP BY i.issue_type
                ORDER BY cnt DESC
                LIMIT 5;
            """)
            top5 = cur.fetchall()
    except Exception as exc:
        return f"Resumen no disponible: {exc}"

    if not top5:
        return "✅ *[SRE DAILY_REPORT]*\nSin anomalías detectadas en las últimas 24 h."

    lines = [
        "📊 *[SRE DAILY_REPORT]* Reporte Diario (últimas 24h)",
        f"Total: {total} incidentes  |  Auto-reparados: {healed}  |  Notificados: {notified}",
        "",
        "*Top 5 anomalías:*",
    ]
    for issue_type, cnt, severity, last_detail, last_resource in top5:
        icon = _SEV_ICON.get(severity, "⚪")
        desc = ""
        if last_detail:
            desc = last_detail.split(".")[0].strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
        resource_short = (last_resource or "").split("/")[-1][:30]
        lines.append(f"{icon} *{issue_type}* ×{cnt}")
        if desc:
            lines.append(f"   └ {desc}")
        if resource_short:
            lines.append(f"   └ Último: {resource_short}")
    return "\n".join(lines)


# ── Morning briefing (P7-S1) ──────────────────────────────────────────────────

def generate_morning_briefing() -> str:
    """Briefing matutino: SLO vivo, salud del cluster, top memoria, predicciones."""
    from agents.sre.observer import _OBSERVE_NAMESPACES, _prometheus_query

    now = datetime.now(UTC)
    prom = _prometheus_url()
    lines = [f"☀️ *Amael SRE — Morning Briefing* ({now.strftime('%d %b %Y')})"]

    # 1. SLO en vivo desde Prometheus
    slo_lines = []
    for slo in _slo_targets():
        handler  = slo.get("handler", slo.get("service", "?"))
        target   = slo.get("availability", 0)
        window_h = slo.get("window_hours", 24)
        q = (
            f'1 - sum(rate(amael_http_requests_total{{handler=~"{handler}",'
            f'status_code=~"5..",namespace="amael-ia"}}[{window_h}h])) '
            f'/ sum(rate(amael_http_requests_total{{handler=~"{handler}",'
            f'namespace="amael-ia"}}[{window_h}h]))'
        )
        results = _prometheus_query(prom, q)
        if results:
            avail = float(results[0]["value"][1])
            icon  = "✅" if avail >= target else "🔴"
            slo_lines.append(f"  {icon} {handler}: {avail:.2%} (target {target:.1%})")
        else:
            slo_lines.append(f"  ⚪ {handler}: sin datos")
    if slo_lines:
        lines.append("\n📊 *SLO (últimas 24h):*")
        lines.extend(slo_lines)

    # 2. Salud del cluster (pods)
    total_pods, healthy_pods, problem_list = 0, 0, []
    try:
        from kubernetes import client as k8s

        from agents.sre.observer import _get_k8s_client
        _get_k8s_client()
        v1 = k8s.CoreV1Api()
        for ns in _OBSERVE_NAMESPACES:
            try:
                for pod in v1.list_namespaced_pod(ns).items:
                    total_pods += 1
                    phase = pod.status.phase or "Unknown"
                    if phase in ("Running", "Succeeded"):
                        healthy_pods += 1
                    else:
                        short = "-".join(pod.metadata.name.split("-")[:-2]) or pod.metadata.name
                        problem_list.append(f"{ns}/{short}")
            except Exception:
                pass
        lines.append(f"\n🖥️ *Cluster:* {healthy_pods}/{total_pods} pods ✅")
        if problem_list:
            lines.append(f"  ⚠️ Problemas: {', '.join(problem_list[:4])}")
    except Exception as exc:
        lines.append(f"\n🖥️ *Cluster:* error consultando pods ({exc})")

    # 3. Top 3 pods por % de memoria vs. límite
    ns_regex = "|".join(_OBSERVE_NAMESPACES)
    q = (
        f'topk(3, sum by (pod, namespace) ('
        f'container_memory_working_set_bytes{{container!="",namespace=~"{ns_regex}"}}'
        f') / sum by (pod, namespace) ('
        f'kube_pod_container_resource_limits{{resource="memory",container!="",namespace=~"{ns_regex}"}}'
        f'))'
    )
    results = _prometheus_query(prom, q)
    if results:
        lines.append("\n💾 *Top memoria (% del límite):*")
        for r in results:
            pod = r["metric"].get("pod", "?")
            val = float(r["value"][1]) * 100
            icon = "⚠️" if val > 80 else ("📢" if val > 65 else "✅")
            short = "-".join(pod.split("-")[:-2]) or pod
            lines.append(f"  {icon} {short}: {val:.0f}%")

    # 4. Predicciones de disco (multi-nivel)
    pred_lines = []
    for days, severity_icon in [(1, "🚨"), (3, "⚠️"), (7, "📢")]:
        disk_q = (
            f'predict_linear(node_filesystem_avail_bytes{{mountpoint="/"}}[6h], '
            f'{days * 24} * 3600) < 0'
        )
        for r in (_prometheus_query(prom, disk_q) or []):
            instance = r["metric"].get("instance", "nodo")
            pred_lines.append(f"  {severity_icon} Disco {instance}: agota en <{days}d")
    if pred_lines:
        lines.append("\n🔮 *Predicciones:*")
        lines.extend(pred_lines)
    else:
        lines.append("\n🔮 *Predicciones:* Sin alertas ✅")

    # 5. Incidentes nocturnos (últimas 8h)
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE action_taken='ROLLOUT_RESTART') AS healed
                FROM sre_incidents
                WHERE created_at > now() - INTERVAL '8 hours'
            """)
            row = cur.fetchone()
            total_night, healed_night = (row[0] or 0, row[1] or 0) if row else (0, 0)
        icon_n = "✅" if total_night == 0 else "⚠️"
        lines.append(
            f"\n🌙 *Noche (8h):* {icon_n} {total_night} incidentes, "
            f"{healed_night} auto-resueltos"
        )
    except Exception:
        pass

    # 6. Circuit breaker y aprobaciones pendientes
    try:
        from agents.sre.scheduler import get_loop_state
        cb = get_loop_state().circuit_breaker_state
        if cb.upper() != "CLOSED":
            lines.append(f"\n⚡ *Circuit Breaker:* {cb} ⚠️ — revisa el loop")
    except Exception:
        pass
    try:
        from agents.sre.approvals import list_pending_approvals
        pending = list_pending_approvals()
        if pending:
            lines.append(f"\n⏳ *Aprobaciones pendientes:* {len(pending)}")
            for ap in pending[:2]:
                lines.append(
                    f"  • {ap['action']} en `{ap['resource']}` — responde *sre si* / *sre no*"
                )
    except Exception:
        pass

    return "\n".join(lines)


# ── Retrospectiva semanal (P7) ────────────────────────────────────────────────

def generate_weekly_retrospective() -> str:
    """Retrospectiva semanal: totales, top patrones y recomendaciones LLM."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE action_taken = 'ROLLOUT_RESTART'
                                       AND action_result LIKE '%%✅%%') AS healed,
                    COUNT(*) FILTER (WHERE action_taken = 'NOTIFY_HUMAN') AS notified,
                    COUNT(*) FILTER (WHERE action_taken = 'SCALE_UP') AS scaled,
                    COUNT(*) FILTER (WHERE action_taken = 'PATCH_RESOURCES') AS patched,
                    ROUND(AVG(confidence)::numeric, 2) AS avg_confidence
                FROM sre_incidents
                WHERE created_at > now() - INTERVAL '7 days';
            """)
            row = cur.fetchone() or (0, 0, 0, 0, 0, 0)
            total, healed, notified, scaled, patched, avg_conf = row

            cur.execute("""
                SELECT issue_type,
                       COUNT(*) AS cnt,
                       MAX(severity) AS max_sev,
                       COUNT(*) FILTER (WHERE action_taken='ROLLOUT_RESTART'
                                          AND action_result LIKE '%%✅%%') AS auto_fixed,
                       (SELECT resource_name FROM sre_incidents i2
                         WHERE i2.issue_type = i.issue_type
                           AND i2.created_at > now() - INTERVAL '7 days'
                         ORDER BY i2.created_at DESC LIMIT 1) AS last_resource
                FROM sre_incidents i
                WHERE created_at > now() - INTERVAL '7 days'
                GROUP BY issue_type
                ORDER BY cnt DESC
                LIMIT 5;
            """)
            top5 = cur.fetchall()

            cur.execute("""
                SELECT resource_name, COUNT(*) AS cnt
                FROM sre_incidents
                WHERE created_at > now() - INTERVAL '7 days'
                GROUP BY resource_name
                ORDER BY cnt DESC
                LIMIT 3;
            """)
            top_resources = cur.fetchall()

            cur.execute("""
                SELECT TO_CHAR(created_at AT TIME ZONE 'America/Mexico_City', 'Dy') AS dow,
                       COUNT(*) AS cnt
                FROM sre_incidents
                WHERE created_at > now() - INTERVAL '7 days'
                GROUP BY dow
                ORDER BY cnt DESC
                LIMIT 3;
            """)
            busy_days = cur.fetchall()
    except Exception as exc:
        return f"Error generando retrospectiva: {exc}"

    if total == 0:
        return ("📊 *[SRE WEEKLY_RETRO]*\n"
                "Semana sin incidentes detectados. Sistema estable. ✅")

    heal_rate = (healed / total * 100) if total else 0

    lines = [
        f"📊 *[SRE Weekly Retro]* — Semana {datetime.now(UTC).strftime('%d %b %Y')}",
        "",
        f"*Resumen ({total} incidentes):*",
        f"  ✅ Auto-resueltos:   {healed} ({heal_rate:.0f}%)",
        f"  👤 Requirieron humano: {notified}",
        f"  📈 Escalados:         {scaled}",
        f"  🔧 Parcheos memoria:  {patched}",
        f"  🎯 Confianza promedio: {float(avg_conf or 0):.0%}",
    ]

    if top5:
        lines.append("\n*Top anomalías:*")
        for issue_type, cnt, max_sev, auto_fixed, last_res in top5:
            icon  = _SEV_ICON.get(max_sev, "⚪")
            ratio = f"{auto_fixed}/{cnt}" if auto_fixed else f"0/{cnt} ⚠️"
            short = (last_res or "").split("/")[-1]
            short = "-".join(short.split("-")[:-2]) or short
            lines.append(f"  {icon} *{issue_type}* ×{cnt} — auto-fix: {ratio}")
            if short:
                lines.append(f"     └ Último: {short}")

    if top_resources:
        lines.append("\n*Servicios más afectados:*")
        for res, cnt in top_resources:
            short = "-".join(res.split("-")[:-2]) or res
            lines.append(f"  • {short}: {cnt} incidentes")

    if busy_days:
        days_str = ", ".join(f"{d}({c})" for d, c in busy_days)
        lines.append(f"\n*Días más activos:* {days_str}")

    # Recomendaciones LLM
    if top5:
        top_issues_txt = ", ".join(f"{r[0]}×{r[1]}" for r in top5)
        top_res_txt    = ", ".join(r[0].split("/")[-1] for r in (top_resources or []))
        prompt = (
            f"Eres un SRE senior. Esta semana el cluster tuvo {total} incidentes.\n"
            f"Top anomalías: {top_issues_txt}.\n"
            f"Servicios más afectados: {top_res_txt}.\n"
            f"Tasa de auto-heal: {heal_rate:.0f}%.\n\n"
            f"Da exactamente 2-3 recomendaciones concretas y accionables para la "
            f"próxima semana. Sé específico y breve. Responde en español. Sin introducción."
        )
        try:
            from agents.base.llm_factory import get_chat_llm
            # tier="deep": la retrospectiva corre los lunes 08:00 (hora de
            # México), en plena actividad. Generarla en la instancia CPU evita
            # desalojar el modelo interactivo de la VRAM. Cae a la instancia
            # normal si el tier profundo no está configurado.
            resp = get_chat_llm(tier="deep").invoke(prompt)
            recs = (getattr(resp, "content", None) or str(resp)).strip()
            if recs:
                lines.append("\n🤖 *Recomendaciones IA:*")
                for line in recs.splitlines()[:6]:
                    if line.strip():
                        lines.append(f"  {line.strip()}")
        except Exception as exc:
            logger.debug(f"[briefing] LLM error en retro: {exc}")

    lines.append("\n_Próxima retrospectiva: lunes 8am_")
    return "\n".join(lines)


# ── Senders (usados por los cron jobs del scheduler) ──────────────────────────

def send_daily_report() -> None:
    from agents.sre.reporter import notify_whatsapp_sre
    notify_whatsapp_sre(generate_daily_summary(), "HIGH")


def send_morning_briefing() -> None:
    from agents.sre.reporter import notify_whatsapp_sre
    notify_whatsapp_sre(generate_morning_briefing(), "HIGH")


def send_weekly_retrospective() -> None:
    from agents.sre.reporter import notify_whatsapp_sre
    notify_whatsapp_sre(generate_weekly_retrospective(), "HIGH")
