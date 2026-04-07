"""
Camael Analyzer — razonamiento LLM para decidir la estrategia óptima de GitOps fix.

Recibe el contexto rico del handoff Raphael→Camael (logs, métricas, manifiesto actual,
historial de incidentes similares) y produce una FixDecision con:
  - multiplier      : factor de escalado de recursos (1.5–6.0)
  - reasoning       : justificación del multiplicador
  - operator_note   : nota para el operador humano
  - risk_level      : LOW | MEDIUM | HIGH
  - is_temporary    : si el fix es un parche temporal
  - alternative     : fix alternativo recomendado si este falla
  - pr_title        : título del PR (conventional commits)

Se invoca desde CamaelAgent._gitops_fix() antes de aplicar el patch determinístico.
Si el LLM falla, FixDecision retorna valores por defecto conservadores.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("agents.devops.camael_analyzer")

# ── Prompt ────────────────────────────────────────────────────────────────────

_ANALYSIS_PROMPT = """Eres Camael, un agente DevOps autónomo especializado en Kubernetes.
Debes analizar un incidente y decidir la mejor estrategia de fix.

## Incidente detectado
- Tipo: {issue_type}
- Descripción: {issue_desc}
- Servicio: {resource_name} (namespace: {namespace})
- Confianza del diagnóstico (Raphael SRE): {confidence:.0%}
- Reinicios recientes: {restart_count}

## Contexto del manifest actual
{manifest_context}

## Métricas en tiempo real
{metrics_context}

## Logs recientes del pod
{logs_context}

## Historial de incidentes similares
{history_context}

## Tu tarea
Razona y decide:
1. ¿Cuál es el factor de escalado óptimo? (considera el valor actual, uso real, y el patrón del incidente)
2. ¿Este fix es suficiente o es solo un parche temporal? ¿Qué debería investigarse además?
3. ¿Cuál es el nivel de riesgo del cambio? (LOW/MEDIUM/HIGH)
4. ¿Qué fix alternativo recomendarías si este no funciona?
5. Escribe una nota concisa para el operador humano explicando tu razonamiento.

Responde en JSON exactamente con esta estructura:
{{
  "multiplier": <float entre 1.5 y 6.0>,
  "reasoning": "<1-2 oraciones explicando por qué este multiplicador específico>",
  "operator_note": "<nota para el operador: qué hace el fix, riesgos y siguiente paso recomendado>",
  "risk_level": "<LOW|MEDIUM|HIGH>",
  "is_temporary": <true|false>,
  "alternative": "<fix alternativo si este falla>",
  "pr_title": "<título conciso para el PR en inglés, estilo conventional commits>"
}}"""


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FixDecision:
    """Decisión de fix producida por el LLM analyzer."""
    multiplier:    float = 2.0
    reasoning:     str   = ""
    operator_note: str   = ""
    risk_level:    str   = "MEDIUM"
    is_temporary:  bool  = True
    alternative:   str   = ""
    pr_title:      str   = ""
    llm_used:      bool  = False


# ── LLM singleton ─────────────────────────────────────────────────────────────

_analyzer_llm = None


def _get_analyzer_llm():
    global _analyzer_llm
    if _analyzer_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _analyzer_llm = get_chat_llm(temperature=0)  # determinístico para decisiones
    return _analyzer_llm


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _fetch_similar_incidents(issue_type: str, resource_name: str, limit: int = 3) -> str:
    """
    Recupera incidentes similares de PostgreSQL para dar contexto histórico al LLM.
    Retorna string vacío si falla (no bloquea el análisis).
    """
    try:
        from storage.postgres.pool import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT issue_type, resource_name, action_taken, resolution_status,
                   created_at
            FROM sre_incidents
            WHERE issue_type = $1
              AND (resource_name = $2 OR $2 = '')
            ORDER BY created_at DESC
            LIMIT $3
            """,
            issue_type, resource_name, limit,
        )
        if not rows:
            # Fallback: same issue_type, any resource
            rows = await pool.fetch(
                """
                SELECT issue_type, resource_name, action_taken, resolution_status,
                       created_at
                FROM sre_incidents
                WHERE issue_type = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                issue_type, limit,
            )
        if not rows:
            return "- Sin incidentes similares en el historial."

        lines = []
        for r in rows:
            ts   = str(r["created_at"])[:16] if r["created_at"] else "?"
            res  = r["resolution_status"] or "?"
            act  = r["action_taken"] or "?"
            lines.append(
                f"- [{ts}] {r['issue_type']} en {r['resource_name']}: "
                f"acción={act}, resultado={res}"
            )
        return "\n".join(lines)

    except Exception as exc:
        logger.debug(f"[camael_analyzer] _fetch_similar_incidents error: {exc}")
        return "- Historial no disponible."


def _summarize_manifest(yaml_content: str) -> str:
    """
    Extrae las secciones relevantes del YAML (resources, limits, replicas)
    para no saturar el prompt con el manifiesto completo.
    Retorna las primeras 1500 chars si no puede parsear.
    """
    if not yaml_content:
        return "- Manifest no disponible."
    try:
        import yaml  # type: ignore
        docs = list(yaml.safe_load_all(yaml_content))
        summary_lines = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind", "?")
            name = (doc.get("metadata") or {}).get("name", "?")
            summary_lines.append(f"kind: {kind}  name: {name}")

            spec = doc.get("spec", {}) or {}
            # Réplicas
            replicas = spec.get("replicas")
            if replicas is not None:
                summary_lines.append(f"  replicas: {replicas}")

            # Containers resources
            template = (spec.get("template") or {})
            containers = (template.get("spec") or {}).get("containers", [])
            for c in containers:
                c_name = c.get("name", "?")
                res = c.get("resources", {}) or {}
                req = res.get("requests", {}) or {}
                lim = res.get("limits", {}) or {}
                summary_lines.append(
                    f"  container '{c_name}': "
                    f"requests={{cpu={req.get('cpu','?')}, memory={req.get('memory','?')}}}, "
                    f"limits={{cpu={lim.get('cpu','?')}, memory={lim.get('memory','?')}}}"
                )
        return "\n".join(summary_lines) if summary_lines else yaml_content[:1500]
    except Exception:
        return yaml_content[:1500]


# ── Issue descriptions ────────────────────────────────────────────────────────

#: Descripciones por tipo de anomalía para enriquecer el prompt del LLM.
_ISSUE_DESCRIPTIONS: dict[str, str] = {
    "OOM_KILLED":             "El pod fue terminado por el kernel por exceder el límite de memoria (OOMKilled). El límite actual es insuficiente para la carga de trabajo.",
    "CRASH_LOOP":             "El contenedor entra en bucle de reinicios (CrashLoopBackOff). Puede deberse a recursos insuficientes, fallo de startup probe, o error de aplicación.",
    "DEPLOYMENT_DEGRADED":    "El Deployment tiene menos réplicas disponibles que las deseadas durante un período prolongado.",
    "HIGH_MEMORY":            "El uso de memoria del pod supera el umbral crítico de forma sostenida, riesgo de OOMKill inminente.",
    "HIGH_CPU":               "El uso de CPU del pod supera el umbral crítico, causando throttling y degradación de latencia.",
    "HIGH_RESTARTS":          "El pod acumula un número alto de reinicios recientes, indicando inestabilidad crónica.",
    "MEMORY_LEAK_PREDICTED":  "La tendencia de crecimiento de memoria predice agotamiento del límite en el corto plazo (predict_linear).",
    "POD_FAILED":             "El pod terminó con código de error. Analiza los logs para determinar la causa y el fix óptimo (recursos insuficientes, probe delay muy agresiva, o configuración incorrecta).",
}


# ── Main public API ───────────────────────────────────────────────────────────

async def analyze_and_decide(
    issue_type:              str,
    resource_name:           str,
    namespace:               str,
    yaml_content:            str,
    diagnosis:               str = "",
    confidence:              float = 0.0,
    incident_key:            str = "",
    # Contexto rico del handoff
    pod_logs:                str = "",
    restart_count:           int = 0,
    current_memory_usage_mi: int | None = None,
    current_cpu_usage_m:     int | None = None,
) -> FixDecision:
    """
    Invoca el LLM para razonar sobre el incidente y decidir la mejor estrategia de fix.

    Args:
        issue_type:              Tipo de anomalía (OOM_KILLED, CRASH_LOOP, etc.)
        resource_name:           Nombre del deployment afectado.
        namespace:               Namespace Kubernetes.
        yaml_content:            Contenido del manifiesto YAML actual.
        diagnosis:               Diagnóstico de texto generado por Raphael.
        confidence:              Confianza del diagnóstico (0.0–1.0).
        incident_key:            Clave única del incidente (para historial).
        pod_logs:                Logs del pod (últimas 50 líneas).
        restart_count:           Número de reinicios recientes del pod.
        current_memory_usage_mi: Uso de memoria actual en MiB (o None).
        current_cpu_usage_m:     Uso de CPU actual en milicores (o None).

    Returns:
        FixDecision con multiplier, reasoning, operator_note, risk_level, etc.
        En caso de fallo LLM retorna FixDecision con valores por defecto conservadores.
    """
    issue_desc = (
        diagnosis
        or _ISSUE_DESCRIPTIONS.get(issue_type)
        or f"Anomalía detectada: {issue_type} en {resource_name}"
    )

    # Contexto del manifest (sección relevante)
    manifest_summary = _summarize_manifest(yaml_content)

    # Historial de incidentes similares
    history_summary = await _fetch_similar_incidents(issue_type, resource_name)

    # Contexto de métricas en tiempo real
    metrics_lines = []
    if current_memory_usage_mi is not None:
        metrics_lines.append(f"- Uso de memoria actual: {current_memory_usage_mi} Mi")
    if current_cpu_usage_m is not None:
        metrics_lines.append(f"- Uso de CPU actual: {current_cpu_usage_m} m")
    metrics_context = (
        "\n".join(metrics_lines)
        if metrics_lines
        else "- Métricas no disponibles en este momento"
    )

    # Logs del pod
    logs_context = (
        f"```\n{pod_logs[:1500]}\n```"
        if pod_logs
        else "- Logs no disponibles (pod ya terminado o error de acceso)"
    )

    prompt = _ANALYSIS_PROMPT.format(
        issue_type=issue_type,
        issue_desc=issue_desc,
        resource_name=resource_name,
        namespace=namespace,
        confidence=confidence,
        restart_count=restart_count,
        manifest_context=manifest_summary,
        metrics_context=metrics_context,
        logs_context=logs_context,
        history_context=history_summary,
    )

    try:
        llm = _get_analyzer_llm()
        raw = await llm.ainvoke(prompt)
        # BaseChatModel retorna AIMessage — extraer content
        if hasattr(raw, "content"):
            raw = raw.content

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning(
                f"[camael_analyzer] LLM no retornó JSON válido para "
                f"{issue_type}/{resource_name}"
            )
            return FixDecision()

        data = json.loads(match.group())
        multiplier = float(data.get("multiplier", 2.0))
        multiplier = max(1.5, min(6.0, multiplier))  # clamp al rango seguro

        decision = FixDecision(
            multiplier    = multiplier,
            reasoning     = str(data.get("reasoning", "")),
            operator_note = str(data.get("operator_note", "")),
            risk_level    = str(data.get("risk_level", "MEDIUM")).upper(),
            is_temporary  = bool(data.get("is_temporary", True)),
            alternative   = str(data.get("alternative", "")),
            pr_title      = str(data.get("pr_title", "")),
            llm_used      = True,
        )
        logger.info(
            f"[camael_analyzer] decision={decision.multiplier}x "
            f"risk={decision.risk_level} temporary={decision.is_temporary} "
            f"incident={incident_key}"
        )
        return decision

    except Exception as exc:
        logger.warning(
            f"[camael_analyzer] LLM analyze_and_decide falló "
            f"(usando defaults): {exc}"
        )
        return FixDecision()
