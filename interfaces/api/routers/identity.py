"""
Router /api/identity — control de acceso y whitelist.

Endpoints:
  GET  /api/identity/check         — verifica si un email/phone tiene acceso
  GET  /api/identity/me            — datos del usuario autenticado (JWT)
  POST /api/identity/pair-code     — genera un código de emparejamiento (1 h)
  POST /api/identity/pair          — canjea un código y da de alta el número
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from interfaces.api.auth import get_current_user, require_internal_secret

logger = logging.getLogger("interfaces.api.identity")

router = APIRouter(prefix="/api/identity", tags=["identity"])


class AccessCheckResponse(BaseModel):
    allowed:            bool
    identifier:         str
    canonical_user_id:  str | None = None
    allow_requests:     bool = False

class UserInfoResponse(BaseModel):
    user_id: str
    allowed: bool


@router.get("/check", response_model=AccessCheckResponse)
def check_access(
    email:      str | None = Query(default=None),
    phone:      str | None = Query(default=None),
    number:     str | None = Query(default=None),
    identifier: str | None = Query(default=None),  # usado por whatsapp-bridge
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

    value = (email or phone or number or identifier or "").strip()
    if not value:
        return AccessCheckResponse(allowed=False, identifier="")

    # Fuente de verdad única: user_identities + user_profile en DB
    # El email principal también vive en user_profile.user_id
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Buscar por identidad secundaria (whatsapp, teléfono)
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
                    logger.debug(f"[identity] check: {value!r} → allowed (identity)")
                    return AccessCheckResponse(
                        allowed=True, identifier=value,
                        canonical_user_id=row[0], allow_requests=False,
                    )
                # Buscar por email directo (user_id = email)
                cur.execute(
                    "SELECT user_id FROM user_profile WHERE user_id = %s AND status = 'active'",
                    (value,),
                )
                row = cur.fetchone()
                if row:
                    logger.debug(f"[identity] check: {value!r} → allowed (profile)")
                    return AccessCheckResponse(
                        allowed=True, identifier=value,
                        canonical_user_id=row[0], allow_requests=False,
                    )
    except Exception as exc:
        logger.error(f"[identity] DB check failed para {value!r}: {exc}")

    logger.debug(f"[identity] check: {value!r} → denied")
    return AccessCheckResponse(
        allowed=False, identifier=value,
        canonical_user_id=None, allow_requests=False,
    )


# ── DM pairing ────────────────────────────────────────────────────────────────
# Dar de alta un número hoy = editar el ConfigMap o el panel admin + redeploy.
# El pairing (patrón Hermes) baja eso a: el admin pide un código, se lo pasa a
# la persona, la persona se lo manda al bot y queda dada de alta — el código
# vive 1 h en Redis y se consume al primer uso.

_PAIR_TTL_S    = 3600
_PAIR_PREFIX   = "pairing:"
# El código se genera con secrets, nunca con random: es una credencial.
_PAIR_CODE_LEN = 4          # bytes → 8 hex chars


class PairCodeResponse(BaseModel):
    code:       str
    expires_in: int
    role:       str


class PairRequest(BaseModel):
    code:  str
    phone: str


class PairResponse(BaseModel):
    canonical_user_id: str
    role:              str


def _redis():
    from storage.redis.client import get_redis_client
    return get_redis_client()


@router.post("/pair-code", response_model=PairCodeResponse)
def create_pair_code(
    user_id: Annotated[str, Depends(get_current_user)],
) -> PairCodeResponse:
    """
    Genera un código de emparejamiento. Solo admins: quien puede dar de alta
    gente es quien ya podía hacerlo por el panel.
    """
    import json
    import secrets

    from storage.postgres.client import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM user_profile WHERE user_id = %s AND status = 'active'",
                (user_id,),
            )
            row = cur.fetchone()
    if not row or row[0] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Solo un admin puede generar códigos de pairing")

    codigo = f"AMAEL-{secrets.token_hex(_PAIR_CODE_LEN).upper()}"
    _redis().setex(
        f"{_PAIR_PREFIX}{codigo}", _PAIR_TTL_S,
        json.dumps({"role": "user", "created_by": user_id}),
    )
    logger.info(f"[identity] Código de pairing generado por {user_id}")
    return PairCodeResponse(code=codigo, expires_in=_PAIR_TTL_S, role="user")


@router.post("/pair", response_model=PairResponse,
             dependencies=[Depends(require_internal_secret)])
def redeem_pair_code(body: PairRequest) -> PairResponse:
    """
    Canjea un código (llamado por el bridge cuando un número desconocido manda
    algo con pinta de código). El GETDEL hace el canje atómico: dos números
    mandando el mismo código a la vez no pueden ganar los dos.
    """
    import json

    codigo = body.code.strip().upper()
    phone  = body.phone.strip()
    if not codigo or not phone:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="code y phone requeridos")

    raw = _redis().getdel(f"{_PAIR_PREFIX}{codigo}")
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Código inválido o expirado")
    datos = json.loads(raw)
    role  = datos.get("role", "user")

    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            # El teléfono es el user_id canónico: no hay email todavía. Si la
            # persona luego entra por Google OAuth, el admin puede fusionar
            # identidades desde el panel.
            cur.execute(
                """
                INSERT INTO user_profile (user_id, display_name, role, status)
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (user_id) DO UPDATE SET status = 'active'
                """,
                (phone, f"WhatsApp {phone[-4:]}", role),
            )
            cur.execute(
                """
                INSERT INTO user_identities (canonical_user_id, identity_type, identity_value)
                SELECT %s, 'whatsapp', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM user_identities
                    WHERE identity_type = 'whatsapp' AND identity_value = %s
                )
                """,
                (phone, phone, phone),
            )
        conn.commit()

    logger.info(
        f"[identity] Pairing canjeado: {phone} dado de alta "
        f"(código de {datos.get('created_by')})"
    )
    return PairResponse(canonical_user_id=phone, role=role)


@router.get("/me", response_model=UserInfoResponse)
def get_me(
    user_id: Annotated[str, Depends(get_current_user)],
) -> UserInfoResponse:
    """Retorna el user_id del JWT activo y si tiene status='active' en la DB."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_profile WHERE user_id = %s AND status = 'active'",
                    (user_id,),
                )
                allowed = cur.fetchone() is not None
    except Exception as exc:
        logger.warning(f"[identity] /me DB check failed para {user_id!r}: {exc}")
        allowed = False
    return UserInfoResponse(user_id=user_id, allowed=allowed)
