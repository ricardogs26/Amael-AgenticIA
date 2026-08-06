"""
Generador de RFC ITIL v4 para cambios de emergencia autónomos.

Produce el payload completo para ServiceNow change_request con:
  - Justificación de negocio
  - Análisis de impacto
  - Plan de implementación
  - Plan de rollback
  - Criterios de éxito
  - Referencias CI/CD
"""
from __future__ import annotations

from datetime import datetime, timedelta

from agents.devops.servicenow_client import RFCState


# ── Mapeos ITIL v4 ────────────────────────────────────────────────────────────

_RISK_MAP: dict[str, tuple[str, str, str]] = {
    # issue_type → (risk, impact, priority)
    "OOM_KILLED":           ("moderate",  "2",  "2 - High"),
    "CRASH_LOOP":           ("high",      "1",  "1 - Critical"),
    "DEPLOYMENT_DEGRADED":  ("moderate",  "2",  "2 - High"),
    "HIGH_MEMORY":          ("moderate",  "2",  "2 - High"),
    "HIGH_CPU":             ("low",       "3",  "3 - Moderate"),
    "IMAGE_PULL_ERROR":     ("low",       "3",  "3 - Moderate"),
    "POD_FAILED":           ("high",      "1",  "1 - Critical"),
    "HIGH_RESTARTS":        ("moderate",  "2",  "2 - High"),
    "MEMORY_LEAK_PREDICTED":("moderate",  "2",  "2 - High"),
}

_ISSUE_LABELS: dict[str, str] = {
    "OOM_KILLED":            "Out of Memory — el contenedor fue terminado por el kernel por exceder el límite de memoria",
    "CRASH_LOOP":            "CrashLoopBackOff — el contenedor falla repetidamente al iniciar",
    "DEPLOYMENT_DEGRADED":   "Deployment degradado — menos réplicas disponibles de las esperadas",
    "HIGH_MEMORY":           "Uso de memoria elevado — riesgo de OOMKill",
    "HIGH_CPU":              "Uso de CPU elevado — posible degradación de rendimiento",
    "IMAGE_PULL_ERROR":      "Error al descargar imagen — el pod no puede iniciar",
    "POD_FAILED":            "Pod en estado Failed — terminación inesperada",
    "HIGH_RESTARTS":         "Alto número de reinicios — inestabilidad del servicio",
    "MEMORY_LEAK_PREDICTED": "Memory leak detectado por tendencia — acción preventiva",
}


# ── Builder principal ─────────────────────────────────────────────────────────

def build_emergency_rfc(
    issue_type:    str,
    pod_name:      str,
    namespace:     str,
    incident_key:  str,
    fix_summary:   str,
    branch_name:   str,
    pr_url:        str,
    pr_id:         int,
    confidence:    float = 0.0,
    detected_at:   str   = "",
    chg_model:     str   = "",
) -> dict:
    """
    Genera el payload completo para crear un Emergency Change RFC en ServiceNow.
    Cumple con ITIL v4.
    """
    risk, impact, priority = _RISK_MAP.get(issue_type, ("moderate", "2", "2 - High"))
    issue_label = _ISSUE_LABELS.get(issue_type, issue_type)
    now = datetime.utcnow()
    detected_at = detected_at or now.strftime("%Y-%m-%d %H:%M UTC")
    service_name = pod_name.replace("-deployment", "").replace("-service", "")
    planned_start = now.strftime("%Y-%m-%d %H:%M:%S")
    planned_end   = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    description = _build_description(
        issue_type=issue_type,
        issue_label=issue_label,
        pod_name=pod_name,
        namespace=namespace,
        incident_key=incident_key,
        fix_summary=fix_summary,
        branch_name=branch_name,
        pr_url=pr_url,
        pr_id=pr_id,
        confidence=confidence,
        detected_at=detected_at,
        service_name=service_name,
    )

    payload = {
        # ── Identificación ──────────────────────────────────────────────────
        "short_description": (
            f"[AUTO] Emergency Fix: {issue_type} — {service_name} ({namespace})"
        ),
        "description": description,

        # ── Change Model (controla type=emergency en ServiceNow) ────────────
        # chg_model debe enviarse en la CREACIÓN — la BR "Change Model: read only
        # presets" impide modificar type/category después de crear el registro.
        **({"chg_model": chg_model} if chg_model else {}),

        # ── Clasificación ITIL v4 ───────────────────────────────────────────
        "type":        "emergency",
        "category":    "Software",
        "subcategory": "Kubernetes / Containers",
        "risk":        risk,
        "impact":      impact,
        "urgency":     "1",      # 1=Critical — emergencia activa en producción
        "priority":    priority,
        "state":       RFCState.ASSESS,

        # ── Ventana de cambio ───────────────────────────────────────────────
        "start_date": planned_start,
        "end_date":   planned_end,

        # ── Asignación ─────────────────────────────────────────────────────
        "assignment_group": "DevOps",
        "requested_by":     "Raphael (SRE Autónomo)",

        # ── Planes ITIL v4 ─────────────────────────────────────────────────
        "justification": (
            f"El agente SRE autónomo Raphael detectó '{issue_type}' en "
            f"{pod_name} (namespace: {namespace}) con {confidence:.0%} de confianza. "
            f"Descripción técnica: {issue_label}. "
            f"Corrección necesaria para restaurar disponibilidad del servicio."
        ),
        "implementation_plan": (
            f"1. Branch automático creado: {branch_name}\n"
            f"2. Patch aplicado al manifest Kubernetes por Camael DevOps Agent\n"
            f"3. Pull Request #{pr_id} creado en Bitbucket ({pr_url})\n"
            f"4. Operador autoriza vía WhatsApp (ECAB simplificado)\n"
            f"5. Merge a main → ArgoCD detecta cambio → sync automático\n"
            f"6. Post Implementation Review automático (5 min) por Raphael"
        ),
        "backout_plan": (
            f"En caso de fallo post-despliegue:\n"
            f"1. Raphael ejecuta rollout undo automáticamente si detecta degradación\n"
            f"2. kubectl rollout undo deployment/{service_name} -n {namespace}\n"
            f"3. RFC se actualiza con el estado del rollback\n"
            f"4. Notificación inmediata al operador vía WhatsApp"
        ),
        "test_plan": (
            f"Post Implementation Review automático por Raphael (T+5 min):\n"
            f"- Pod en estado Running sin reinicios\n"
            f"- Error rate < 1% (Prometheus)\n"
            f"- SLO de disponibilidad mantenido (>= 99%)\n"
            f"- Sin nuevas alertas en ventana de monitoreo de 5 min"
        ),
        "risk_impact_analysis": (
            f"Riesgo: {risk.upper()} | Impacto: {impact} | Urgencia: Critical\n"
            f"Servicio afectado: {service_name} (namespace: {namespace})\n"
            f"Anomalía detectada: {issue_label}\n"
            f"Confianza del diagnóstico: {confidence:.0%}\n"
            f"Tiempo de inactividad esperado: Mínimo (rolling update sin downtime)"
        ),

        # ── Configuration Item ──────────────────────────────────────────────
        "cmdb_ci": service_name,
    }
    return payload


def _build_description(
    issue_type: str, issue_label: str, pod_name: str, namespace: str,
    incident_key: str, fix_summary: str, branch_name: str, pr_url: str,
    pr_id: int, confidence: float, detected_at: str, service_name: str,
) -> str:
    return f"""## RFC de Cambio de Emergencia — {incident_key}

**Detectado por**: Raphael (Agente SRE Autónomo)
**Fecha de detección**: {detected_at}
**Confianza del diagnóstico**: {confidence:.0%}

---

### 1. Descripción del Cambio

**Problema detectado**: {issue_label}
**Servicio afectado**: {pod_name} (namespace: `{namespace}`)
**Clave de incidente**: `{incident_key}`

{fix_summary}

---

### 2. Justificación (Business Case)

El agente SRE autónomo Raphael detectó la anomalía **{issue_type}** durante su ciclo \
de monitoreo continuo (cada 60 segundos). La corrección es necesaria para:
- Restaurar la disponibilidad normal del servicio `{service_name}`
- Prevenir escalada del incidente
- Mantener los SLOs de disponibilidad acordados

---

### 3. Análisis de Impacto

| Dimensión | Detalle |
|-----------|---------|
| Servicio afectado | `{service_name}` en namespace `{namespace}` |
| Tipo de anomalía | `{issue_type}` |
| Impacto en usuarios | Degradación del servicio durante la anomalía |
| Tiempo de inactividad esperado | Mínimo — rolling update sin downtime |
| Sistemas dependientes | Frontend, WhatsApp Bridge |

---

### 4. Plan de Implementación

1. Branch creado automáticamente: `{branch_name}`
2. Patch aplicado al manifest Kubernetes (YAML) por Camael DevOps Agent
3. Pull Request #{pr_id} creado en Bitbucket
4. Revisión y aprobación por operador autorizado
5. Merge a `main` → ArgoCD detecta cambio → sync automático
6. Verificación post-despliegue (5 min) por Raphael

---

### 5. Plan de Rollback

En caso de fallo post-despliegue:
- Raphael ejecuta `kubectl rollout undo` automáticamente si detecta degradación
- Este RFC se actualiza con el estado del rollback
- Notificación inmediata al operador vía WhatsApp

---

### 6. Criterios de Éxito

- [ ] Pod en estado `Running` sin reinicios en los 5 min siguientes al despliegue
- [ ] Error rate < 1% (verificado en Prometheus)
- [ ] SLO de disponibilidad mantenido (>= 99%)
- [ ] Sin nuevas alertas en ventana de monitoreo de 5 min

---

### 7. Referencias CI/CD

| Campo | Valor |
|-------|-------|
| Repositorio | `amael-agentic-backend` (Bitbucket) |
| Branch | `{branch_name}` |
| Pull Request | [PR #{pr_id}]({pr_url}) |
| ArgoCD App | `amael-agentic-backend` |
| Namespace Kubernetes | `{namespace}` |

---

### 8. Trazabilidad del Agente

| Campo | Valor |
|-------|-------|
| Agente detector | Raphael (SRE Autónomo) |
| Agente ejecutor | Camael (DevOps Agent) |
| Clave de incidente | `{incident_key}` |
| Confianza diagnóstico | {confidence:.0%} |
| Fecha detección | {detected_at} |

*Este RFC fue generado automáticamente por el sistema de agentes Amael-AgenticIA.*
""".strip()
