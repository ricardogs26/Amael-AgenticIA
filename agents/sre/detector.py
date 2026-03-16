"""
SRE Detector — detecta y correlaciona anomalías del clúster.

Orquesta las observaciones de observer.py para producir una
lista consolidada de anomalías únicas, priorizadas y correlacionadas.

Migrado desde k8s-agent/main.py:
  detect_anomalies()      — combina observaciones estructurales + métricas
  correlate_anomalies()   — agrupa anomalías multi-pod del mismo deployment (P4-B)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from agents.sre.models import Anomaly
from core.constants import AnomalyType, Severity
from observability.metrics import SRE_ANOMALIES_DETECTED_TOTAL

logger = logging.getLogger("agents.sre.detector")

# Orden de prioridad para reporting (mayor index = mayor prioridad)
_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def detect_anomalies(
    structural: List[Anomaly],
    metric: Optional[List[Anomaly]] = None,
    trend: Optional[List[Anomaly]] = None,
    slo: Optional[List[Anomaly]] = None,
) -> List[Anomaly]:
    """
    Combina todas las fuentes de observación en una lista consolidada.

    Orden de prioridad:
      1. SLO violations (CRITICAL)
      2. Structural anomalies (HIGH)
      3. Metric anomalies (MEDIUM/HIGH)
      4. Predictive trends (MEDIUM)

    Registra métricas Prometheus por tipo y severidad.
    """
    all_anomalies: List[Anomaly] = []
    all_anomalies.extend(slo or [])
    all_anomalies.extend(structural)
    all_anomalies.extend(metric or [])
    all_anomalies.extend(trend or [])

    # Deduplicar por incident_key (misma anomalía detectada por múltiples fuentes)
    seen: Dict[str, Anomaly] = {}
    for anomaly in all_anomalies:
        key = anomaly.incident_key
        if key not in seen:
            seen[key] = anomaly
        else:
            # Preservar la versión con mayor severidad
            existing = seen[key]
            if _SEVERITY_RANK.get(anomaly.severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
                seen[key] = anomaly

    unique = list(seen.values())

    # Ordenar por severidad descendente
    unique.sort(
        key=lambda a: _SEVERITY_RANK.get(a.severity, 0),
        reverse=True,
    )

    # Registrar métricas
    for anomaly in unique:
        SRE_ANOMALIES_DETECTED_TOTAL.labels(
            severity=anomaly.severity,
            issue_type=anomaly.issue_type,
        ).inc()

    if unique:
        logger.info(
            f"[detector] {len(unique)} anomalías únicas detectadas "
            f"(structural={len(structural)}, metric={len(metric or [])}, "
            f"trend={len(trend or [])}, slo={len(slo or [])})"
        )

    return unique


def correlate_anomalies(anomalies: List[Anomaly]) -> List[Anomaly]:
    """
    Agrupa anomalías de múltiples pods del mismo deployment (P4-B).

    Cuando 3 o más pods del mismo owner tienen el mismo issue_type,
    los consolida en una sola anomalía a nivel de Deployment con
    severidad CRITICAL para evitar spam de notificaciones.

    Migrado desde k8s-agent/main.py → correlate_anomalies()
    """
    from collections import defaultdict
    try:
        from observability.metrics import SRE_CORRELATION_GROUPED
    except ImportError:
        SRE_CORRELATION_GROUPED = None

    # Agrupar por (owner_name, namespace, issue_type)
    groups: Dict[tuple, List[Anomaly]] = defaultdict(list)
    ungrouped: List[Anomaly] = []

    for anomaly in anomalies:
        if anomaly.owner_name and anomaly.resource_type == "Pod":
            key = (anomaly.owner_name, anomaly.namespace, anomaly.issue_type)
            groups[key].append(anomaly)
        else:
            ungrouped.append(anomaly)

    result = list(ungrouped)
    for (owner, ns, issue_type), group in groups.items():
        if len(group) >= 3:
            # Correlacionar: crear anomalía de nivel Deployment
            try:
                from observability.metrics import SRE_CORRELATION_GROUPED
                SRE_CORRELATION_GROUPED.inc()
            except Exception:
                pass

            logger.info(
                f"[detector] Correlación: {len(group)} pods de '{owner}' "
                f"con {issue_type} agrupados."
            )
            correlated = Anomaly(
                issue_type=issue_type,
                severity=Severity.CRITICAL,
                namespace=ns,
                resource_name=owner,
                resource_type="Deployment",
                owner_name=owner,
                details=(
                    f"{len(group)} pods de {owner} con {issue_type}. "
                    f"Pods afectados: {', '.join(a.resource_name for a in group[:5])}"
                ),
                metadata={"correlated_pods": len(group)},
            )
            result.append(correlated)
        else:
            result.extend(group)

    # Re-ordenar por severidad
    result.sort(
        key=lambda a: _SEVERITY_RANK.get(a.severity, 0),
        reverse=True,
    )
    return result
