"""
Modelos de datos del SREAgent.

Migrados desde k8s-agent/main.py: dataclasses Anomaly y SREAction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Ancho de la ventana de ocurrencia, en minutos. Dos detecciones del mismo
# problema dentro de la misma ventana son el mismo episodio; en ventanas
# distintas son reincidencias y cada una debe quedar registrada.
SRE_OCCURRENCE_WINDOW_MIN = int(os.environ.get("SRE_OCCURRENCE_WINDOW_MIN", "60"))


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
        default_factory=lambda: datetime.now(UTC)
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def incident_key(self) -> str:
        """
        Clave estable del problema — deduplicación en Redis, approvals y
        handoff GitOps. Sin componente temporal: identifica *qué* falla,
        no *cuándo*.
        """
        return f"{self.namespace}:{self.resource_name}:{self.issue_type}"

    @property
    def occurrence_key(self) -> str:
        """
        Clave del episodio concreto — es la que se persiste en PostgreSQL.

        sre_incidents tiene UNIQUE(incident_key) e inserta con ON CONFLICT DO
        NOTHING. Con una clave sin tiempo, la primera ocurrencia bloqueaba para
        siempre el registro de todas las reincidencias: trader-service tenía una
        sola fila del 28-jul mientras seguía siendo reiniciado en agosto. Eso
        cegaba la auditoría y envenenaba get_historical_success_rate(), que
        alimenta el ajuste de confianza del diagnóstico.

        Derivada de self.timestamp (fijo por instancia), no del reloj actual:
        store_incident() y update_incident_verification() ocurren con minutos de
        diferencia y deben apuntar a la misma fila.
        """
        window = SRE_OCCURRENCE_WINDOW_MIN * 60
        bucket = int(self.timestamp.timestamp() // window) * window
        stamp  = datetime.fromtimestamp(bucket, tz=UTC).strftime("%Y%m%dT%H%M")
        return f"{self.incident_key}:{stamp}"

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
        default_factory=lambda: datetime.now(UTC)
    )


@dataclass
class SRELoopState:
    """
    Estado en tiempo real del loop autónomo SRE.
    Retornado por GET /api/sre/loop/status.
    """
    loop_enabled: bool
    loop_interval_seconds: int
    last_run_at: datetime | None
    last_run_result: str
    anomalies_in_last_run: int
    actions_in_last_run: int
    circuit_breaker_state: str     # "CLOSED" / "OPEN" / "HALF_OPEN"
    maintenance_active: bool
    leader_pod: str
    is_leader: bool
