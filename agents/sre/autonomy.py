"""
SRE Autonomy — bootstrap gradual de autonomía (P8-B, portado del legacy k8s-agent).

El modo vive en Redis (`sre:agent_mode`) y gobierna qué acciones puede
ejecutar el loop autónomo sin aprobación humana:

    observe      — solo detecta y notifica, nunca actúa
    conservative — solo ROLLOUT_RESTART con confianza > 0.90
    standard     — ROLLOUT_RESTART con el umbral normal (default)
    full         — sin restricciones (ROLLOUT_RESTART, SCALE_UP, PATCH_RESOURCES)
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("agents.sre.autonomy")

_AGENT_MODE_KEY = "sre:agent_mode"

VALID_MODES = {"observe", "conservative", "standard", "full"}

MODE_DESCRIPTIONS = {
    "observe":      "OBSERVE — Solo detecta y notifica. No ejecuta ninguna acción autónoma.",
    "conservative": "CONSERVATIVE — Actúa solo con confianza >90% y solo ROLLOUT_RESTART. "
                    "Ideal para las primeras semanas.",
    "standard":     "STANDARD — Actúa con confianza >75% con ROLLOUT_RESTART. "
                    "Modo recomendado post-validación.",
    "full":         "FULL — Operación completa: ROLLOUT_RESTART, SCALE_UP, "
                    "PATCH_RESOURCES con umbrales normales.",
}


def get_agent_mode() -> str:
    """Modo actual desde Redis. Default: env SRE_INITIAL_MODE o 'standard'."""
    try:
        from storage.redis.client import get_client
        mode = get_client().get(_AGENT_MODE_KEY)
        if mode in VALID_MODES:
            return mode
    except Exception:
        pass
    return os.environ.get("SRE_INITIAL_MODE", "standard")


def set_agent_mode(mode: str) -> bool:
    """Persiste el modo en Redis. Retorna False si el modo es inválido o Redis falla."""
    if mode not in VALID_MODES:
        return False
    try:
        from storage.redis.client import get_client
        get_client().set(_AGENT_MODE_KEY, mode)
        logger.info(f"[autonomy] Modo del agente cambiado a: {mode}")
        return True
    except Exception as exc:
        logger.error(f"[autonomy] Error guardando modo: {exc}")
        return False


def apply_mode_to_action(action: str, confidence: float) -> str:
    """
    Aplica la política del modo actual a una acción propuesta.
    Retorna la acción (posiblemente degradada a OBSERVE_ONLY / NOTIFY_HUMAN).
    """
    mode = get_agent_mode()
    if mode == "observe":
        return "OBSERVE_ONLY"
    # DELETE_STUCK_POD es limpieza segura de cadáveres (ContainerStatusUnknown):
    # permitida en cualquier modo que actúe — no hay app viva que afectar.
    if action == "DELETE_STUCK_POD":
        return action
    if mode == "conservative":
        if action != "ROLLOUT_RESTART" or confidence < 0.90:
            return "OBSERVE_ONLY"
    if mode == "standard":
        if action not in ("ROLLOUT_RESTART", "NOTIFY_HUMAN", "NO_ACTION"):
            return "NOTIFY_HUMAN"
    # full: sin restricciones
    return action
