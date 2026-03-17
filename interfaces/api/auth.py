"""
Dependencias de autenticación y autorización para la API.

Provee:
  - get_current_user(token)  — decodifica JWT → email del usuario
  - require_internal_secret  — verifica INTERNAL_API_SECRET (CronJobs / WhatsApp bridge)
  - check_rate_limit(user_id)— 15 req / 60s por usuario via Redis
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("interfaces.api.auth")

_bearer = HTTPBearer(auto_error=False)


# ── JWT ───────────────────────────────────────────────────────────────────────

def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """
    Dependencia FastAPI: extrae el email del usuario desde el JWT Bearer.
    Lanza HTTP 401 si el token es inválido o ausente.

    Returns:
        Email del usuario (sub del JWT).
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        from jose import jwt

        from config.settings import settings

        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str | None = payload.get("sub") or payload.get("email")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido: falta 'sub'",
            )
        return user_id

    except Exception as exc:
        logger.warning(f"[auth] JWT inválido: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_internal_secret(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """
    Dependencia para endpoints internos (CronJobs, WhatsApp bridge).
    Verifica el header Authorization: Bearer {INTERNAL_API_SECRET}.
    Lanza HTTP 403 si el secret no coincide.
    """
    from config.settings import settings

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token or token != settings.internal_api_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado: secret interno inválido",
        )


# ── Rate limiting ─────────────────────────────────────────────────────────────

def check_rate_limit(user_id: str) -> None:
    """
    Verifica el rate limit del usuario via Redis.
    15 requests / 60 segundos por user_id.
    Lanza HTTP 429 si se excede el límite.
    """
    try:
        from config.settings import settings
        from observability.metrics import SECURITY_RATE_LIMITED_TOTAL
        from storage.redis.client import get_client

        redis = get_client()
        key   = f"rate_limit:{user_id}"
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, settings.rate_limit_window)

        if count > settings.rate_limit_max:
            SECURITY_RATE_LIMITED_TOTAL.inc()
            logger.warning(f"[auth] Rate limit excedido para usuario: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Demasiadas solicitudes. Límite: "
                    f"{settings.rate_limit_max} por {settings.rate_limit_window}s"
                ),
                headers={"Retry-After": str(settings.rate_limit_window)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Si Redis falla, dejar pasar (fail open para no bloquear usuarios)
        logger.warning(f"[auth] Rate limit check falló (fail open): {exc}")
