"""
Sanitización de outputs de agentes antes de enviarlos al usuario.

Elimina secretos, tokens y credenciales que no deben aparecer
en respuestas al usuario.

Migrado desde backend-ia/agents/security.py.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger("security.sanitizer")

# ── Patrones de redacción ─────────────────────────────────────────────────────

# Vault service tokens (prefijo hvs.)
_VAULT_TOKEN_RE = re.compile(r"\bhvs\.[A-Za-z0-9]{20,}\b")

# JWT tokens (tres segmentos base64url separados por punto)
_JWT_RE = re.compile(
    r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)

# Asignaciones de secretos (password=, secret=, token=, api_key=)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|secret|token|api_key|apikey)\s*[:=]\s*['\"]?"
    r"([A-Za-z0-9+/=!@#$%^&*]{16,})['\"]?"
)

# Claves SSH/PEM (entre -----BEGIN y -----END)
_PEM_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----",
    re.MULTILINE,
)

# IPs privadas con credenciales embebidas (user:pass@host)
_CREDENTIALS_IN_URL_RE = re.compile(
    r"(https?://)[^:@\s]+:[^@\s]+@"
)


def sanitize_output(text: str) -> str:
    """
    Redacta secretos y tokens que no deben aparecer en respuestas al usuario.

    Patrones redactados:
      - Vault tokens (hvs.*)
      - JWT tokens (ey*.ey*.*)
      - Asignaciones de secrets (password=xxx, api_key=xxx)
      - Claves PEM/SSH
      - Credenciales embebidas en URLs (user:pass@host)

    Args:
        text: Texto de respuesta del agente.

    Returns:
        Texto con secretos redactados.
    """
    if not text:
        return text

    original_len = len(text)
    redaction_count = 0

    # Vault tokens
    new_text, n = _VAULT_TOKEN_RE.subn("[VAULT_TOKEN_REDACTED]", text)
    redaction_count += n
    text = new_text

    # JWT tokens
    new_text, n = _JWT_RE.subn("[JWT_REDACTED]", text)
    redaction_count += n
    text = new_text

    # Asignaciones de secretos
    new_text, n = _SECRET_ASSIGNMENT_RE.subn(
        lambda m: f"{m.group(1)}=[REDACTED]", text
    )
    redaction_count += n
    text = new_text

    # Claves PEM/SSH
    new_text, n = _PEM_KEY_RE.subn("[PEM_KEY_REDACTED]", text)
    redaction_count += n
    text = new_text

    # Credenciales en URLs
    new_text, n = _CREDENTIALS_IN_URL_RE.subn(r"\1[CREDENTIALS_REDACTED]@", text)
    redaction_count += n
    text = new_text

    if redaction_count > 0:
        logger.warning(
            "Secretos redactados de la respuesta",
            extra={
                "redaction_count": redaction_count,
                "original_length": original_len,
                "final_length": len(text),
            },
        )

    return text
