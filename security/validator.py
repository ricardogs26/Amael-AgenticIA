"""
Validación de inputs de usuario antes de entrar al pipeline de agentes.

Migrado desde backend-ia/agents/security.py y extendido con
métricas Prometheus centralizadas.
"""
from __future__ import annotations

import re
import logging
from typing import Tuple

from core.constants import MAX_PROMPT_CHARS
from core.exceptions import PromptInjectionError

logger = logging.getLogger("security.validator")

# ── Patrones de prompt injection ──────────────────────────────────────────────
_INJECTION_PATTERNS = re.compile(
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?"
    r"|forget\s+(everything|all|your|the)"
    r"|disregard\s+(all\s+)?(previous|prior|above)"
    r"|you\s+are\s+now\s+(a\s+)?(?:different|new|another)",
    re.IGNORECASE,
)

# ── Caracteres de control a eliminar ─────────────────────────────────────────
# Elimina null bytes y caracteres de control no imprimibles
# pero preserva \n (0x0A), \t (0x09) y \r (0x0D)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_prompt(prompt: str) -> Tuple[bool, str]:
    """
    Valida el prompt del usuario antes de entrar al pipeline de agentes.

    Checks realizados (en orden):
      1. Strip de caracteres de control no imprimibles
      2. Límite de longitud (MAX_PROMPT_CHARS)
      3. Detección de patrones de prompt injection

    Args:
        prompt: Texto del usuario sin procesar.

    Returns:
        Tuple[bool, str]:
          - (True, prompt_limpio)   si el input es válido
          - (False, mensaje_error)  si el input debe ser rechazado

    Efectos secundarios:
        Registra en Prometheus las razones de bloqueo.
    """
    # Importación lazy para evitar import circular al arrancar la app
    from observability.metrics import SECURITY_INPUT_BLOCKED_TOTAL

    # 1. Strip de caracteres de control
    cleaned = _CONTROL_CHARS.sub("", prompt)

    # 2. Límite de longitud
    if len(cleaned) > MAX_PROMPT_CHARS:
        logger.warning(
            "Prompt demasiado largo bloqueado",
            extra={
                "length": len(cleaned),
                "max": MAX_PROMPT_CHARS,
                "preview": cleaned[:80],
            },
        )
        SECURITY_INPUT_BLOCKED_TOTAL.labels(reason="too_long").inc()
        return False, (
            f"El mensaje es demasiado largo ({len(cleaned)} caracteres). "
            f"El máximo permitido es {MAX_PROMPT_CHARS}."
        )

    # 3. Detección de prompt injection
    if _INJECTION_PATTERNS.search(cleaned):
        logger.warning(
            "Patrón de prompt injection detectado",
            extra={"preview": cleaned[:100]},
        )
        SECURITY_INPUT_BLOCKED_TOTAL.labels(reason="injection_detected").inc()
        return False, "El mensaje contiene patrones no permitidos."

    return True, cleaned


def validate_prompt_strict(prompt: str) -> str:
    """
    Versión que lanza excepción en lugar de retornar tuple.
    Útil para pipelines donde se prefiere exception handling.

    Raises:
        PromptInjectionError: Si se detecta injection.
        ValueError: Si el prompt excede el límite de longitud.
    """
    valid, result = validate_prompt(prompt)
    if not valid:
        if "demasiado largo" in result:
            raise ValueError(result)
        raise PromptInjectionError(result)
    return result
