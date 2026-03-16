"""
Modelos de datos del SREAgent.

Migrados desde k8s-agent/main.py: dataclasses Anomaly y SREAction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.constants import ActionType, AnomalyType, Severity


@dataclass
class Anomaly:
    """
    Anomalía detectada en el clúster.

    Generada por:
      - observer.observe_cluster()  — anomalías estructurales (pods/nodos)
      - observer.observe_metrics()  — anomalías de métricas (CPU/mem/errors)
      - observer.observe_trends()   — anomalías predictivas (P5-A)
      - observer.observe_slo()      — violaciones de SLO (P5-C)
    """
    issue_type: str          # AnomalyType value
    severity: str            # Severity value
    namespace: str
    resource_name: str
    resource_type: str       # "Deployment", "Pod", "Node", etc.
    details: str             # Descripción legible del problema
    owner_name: str = ""     # Deployment dueño del pod (si aplica)
    confidence: float = 0.0  # Confianza del diagnóstico (0.0–1.0)
    root_cause: str = ""     # Causa raíz (llenada por el Diagnoser)
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def incident_key(self) -> str:
        """Clave única para deduplicación en Redis y PostgreSQL."""
        return f"{self.namespace}:{self.resource_name}:{self.issue_type}"

    def __str__(self) -> str:
        return (
            f"[{self.severity}] {self.issue_type} — "
            f"{self.namespace}/{self.resource_name}"
        )


@dataclass
class SREAction:
    """
    Acción de remediación decidida por el Healer.
    """
    action_type: str          # ActionType value
    anomaly: Anomaly
    rationale: str            # Por qué se tomó esta acción
    executed: bool = False
    result: str = ""          # "✅ OK" / "❌ Error: ..."
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class SRELoopState:
    """
    Estado en tiempo real del loop autónomo SRE.
    Retornado por GET /api/sre/loop/status.
    """
    loop_enabled: bool
    loop_interval_seconds: int
    last_run_at: Optional[datetime]
    last_run_result: str
    anomalies_in_last_run: int
    actions_in_last_run: int
    circuit_breaker_state: str     # "CLOSED" / "OPEN" / "HALF_OPEN"
    maintenance_active: bool
    leader_pod: str
    is_leader: bool
