"""
Modo sombra de HIGH_RESTARTS.

Calcula qué haría Raphael si decidiera con la evidencia del pod y lo guarda junto
con lo que realmente hizo. No ejecuta, no notifica y no cambia la decisión del
loop: solo mide cuántos avisos se evitarían antes de ampliar la autonomía.

Se enciende con SRE_SHADOW_HIGH_RESTARTS=true. Cualquier falla se traga aquí
porque esta función corre dentro del loop SRE.
"""
from __future__ import annotations

import json
import logging
import os

from agents.sre.models import Anomaly
from core.constants import AnomalyType

logger = logging.getLogger("agents.sre.shadow")

SUPPRESS = "SUPPRESS"
ROLLOUT = "ROLLOUT_RESTART"
ESCALATE = "ESCALATE"

# Reinicios nuevos entre dos observaciones que cuentan como problema activo.
_ACTIVE_RESTARTS = int(os.environ.get("SRE_SHADOW_ACTIVE_RESTARTS", "3"))


def decide(evidence: dict, cluster_burst: bool = False) -> tuple[str, str]:
    """(acción, regla) según la evidencia. Sin evidencia nunca suprime."""
    if cluster_burst:
        return SUPPRESS, "rafaga_cluster"

    reason = evidence.get("last_reason")
    if not reason:
        return ESCALATE, "sin_evidencia"
    if reason == "OOMKilled":
        # Reiniciar no corrige un límite de memoria.
        return ESCALATE, "oom"
    if reason == "Completed" or evidence.get("last_exit_code") == 0:
        return SUPPRESS, "salida_limpia"
    if evidence.get("new_restarts", 0) >= _ACTIVE_RESTARTS:
        return ROLLOUT, "reinicios_activos"
    return SUPPRESS, "reinicio_aislado"


def record_high_restarts(anomaly: Anomaly, actual_action: str) -> bool:
    """Guarda la decisión sombra. True si se escribió; nunca lanza excepciones."""
    if os.environ.get("SRE_SHADOW_HIGH_RESTARTS", "false").lower() != "true":
        return False
    if anomaly.issue_type != AnomalyType.HIGH_RESTARTS:
        return False

    try:
        meta = anomaly.metadata or {}
        evidence = meta.get("restart_evidence") or {}
        action, rule = decide(evidence, cluster_burst=bool(meta.get("cluster_burst")))

        from storage.postgres import get_connection
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sre_shadow_decisions (
                    occurrence_key, namespace, resource_name, owner_name, severity,
                    actual_action, shadow_action, rule, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (occurrence_key) DO NOTHING;
                """,
                (
                    anomaly.occurrence_key,
                    anomaly.namespace,
                    anomaly.resource_name,
                    anomaly.owner_name,
                    anomaly.severity,
                    actual_action,
                    action,
                    rule,
                    json.dumps(evidence),
                ),
            )
        return True
    except Exception as exc:
        logger.warning(f"[shadow] No se pudo registrar la decisión: {exc}")
        return False
