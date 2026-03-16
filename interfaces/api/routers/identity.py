"""
Router /api/identity — control de acceso y whitelist.

Endpoints:
  GET  /api/identity/check         — verifica si un email/phone tiene acceso
  GET  /api/identity/me            — datos del usuario autenticado (JWT)
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.identity")

router = APIRouter(prefix="/api/identity", tags=["identity"])


class AccessCheckResponse(BaseModel):
    allowed:            bool
    identifier:         str
    canonical_user_id:  Optional[str] = None
    allow_requests:     bool = False

class UserInfoResponse(BaseModel):
    user_id: str
    allowed: bool


@router.get("/check", response_model=AccessCheckResponse)
def check_access(
    email:      Optional[str] = Query(default=None),
    phone:      Optional[str] = Query(default=None),
    number:     Optional[str] = Query(default=None),
    identifier: Optional[str] = Query(default=None),  # usado por whatsapp-bridge
) -> AccessCheckResponse:
    """
    Verifica si un email o número de teléfono tiene acceso a la plataforma.
    Usado por whatsapp-bridge en cada mensaje entrante.
    No requiere autenticación JWT.

    Estrategia de búsqueda:
      1. ConfigMap whitelist (ALLOWED_EMAILS_CSV + ALLOWED_NUMBERS_CSV)
      2. Tabla user_identities (usuarios gestionados desde Admin panel)
      3. Tabla user_profile con status = 'active'
    """
    from config.settings import settings

    value = (email or phone or number or identifier or "").strip()
    if not value:
        return AccessCheckResponse(allowed=False, identifier="")

    # Fuente de verdad: tabla user_identities + user_profile activo
    # El Admin panel gestiona los usuarios en la DB — el ConfigMap no es relevante aquí.
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ui.canonical_user_id
                    FROM user_identities ui
                    JOIN user_profile up ON up.user_id = ui.canonical_user_id
                    WHERE ui.identity_value = %s AND up.status = 'active'
                    LIMIT 1
                    """,
                    (value,),
                )
                row = cur.fetchone()
                if row:
                    logger.debug(f"[identity] check: {value!r} → allowed (DB)")
                    return AccessCheckResponse(
                        allowed=True, identifier=value,
                        canonical_user_id=row[0], allow_requests=False,
                    )
    except Exception as exc:
        # Si la DB falla, caer a la whitelist del ConfigMap como último recurso
        logger.warning(f"[identity] DB check failed, fallback to ConfigMap: {exc}")
        if value in settings.full_whitelist:
            return AccessCheckResponse(
                allowed=True, identifier=value,
                canonical_user_id=value, allow_requests=False,
            )

    logger.debug(f"[identity] check: {value!r} → denied")
    return AccessCheckResponse(
        allowed=False, identifier=value,
        canonical_user_id=None, allow_requests=False,
    )


@router.get("/me", response_model=UserInfoResponse)
def get_me(
    user_id: Annotated[str, Depends(get_current_user)],
) -> UserInfoResponse:
    """Retorna el user_id del JWT activo y si está en la whitelist."""
    from config.settings import settings
    allowed = user_id in settings.full_whitelist
    return UserInfoResponse(user_id=user_id, allowed=allowed)
