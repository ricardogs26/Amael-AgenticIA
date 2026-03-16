"""
Router /api/admin — gestión de usuarios y configuración de la plataforma.

Solo accesible por usuarios con role = 'admin'.

Endpoints:
  GET    /api/admin/users                          — lista todos los usuarios
  POST   /api/admin/users                          — crea usuario
  PATCH  /api/admin/users/{uid}                    — actualiza usuario (role/status/display_name)
  DELETE /api/admin/users/{uid}                    — elimina usuario
  POST   /api/admin/users/{uid}/identity           — añade identidad (whatsapp)
  DELETE /api/admin/users/{uid}/identity/{value}   — elimina identidad
  GET    /api/admin/settings                        — obtiene configuración
  PATCH  /api/admin/settings                        — actualiza configuración
"""
from __future__ import annotations

import json
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Dependencia admin ──────────────────────────────────────────────────────────

def require_admin(user_id: Annotated[str, Depends(get_current_user)]) -> str:
    """Verifica que el usuario tenga role = 'admin'. Lanza 403 si no."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM user_profile WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                if not row or row[0] != "admin":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Acceso restringido a administradores",
                    )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] require_admin error: {exc}")
        raise HTTPException(status_code=500, detail="Error verificando permisos")
    return user_id


# ── Modelos ────────────────────────────────────────────────────────────────────

class IdentityOut(BaseModel):
    type:  str
    value: str

class AdminUser(BaseModel):
    user_id:      str
    display_name: Optional[str]
    role:         str
    status:       str
    identities:   List[IdentityOut] = []

class CreateUserRequest(BaseModel):
    email:        str
    display_name: Optional[str] = None
    role:         str = "user"
    phone:        Optional[str] = None

class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    role:         Optional[str] = None
    status:       Optional[str] = None

class AddIdentityRequest(BaseModel):
    identity_type:  str   # "whatsapp"
    identity_value: str

class SettingsUpdate(BaseModel):
    allow_access_requests: Optional[bool] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_users(cur) -> List[AdminUser]:
    cur.execute(
        "SELECT user_id, display_name, role, status FROM user_profile ORDER BY user_id"
    )
    rows = cur.fetchall()

    cur.execute("SELECT canonical_user_id, identity_type, identity_value FROM user_identities")
    idents = cur.fetchall()

    ident_map: dict[str, list] = {}
    for uid, itype, ival in idents:
        ident_map.setdefault(uid, []).append(IdentityOut(type=itype, value=ival))

    return [
        AdminUser(
            user_id=r[0],
            display_name=r[1],
            role=r[2] or "user",
            status=r[3] or "active",
            identities=ident_map.get(r[0], []),
        )
        for r in rows
    ]


def _get_setting(cur, key: str, default=None):
    cur.execute("SELECT value FROM platform_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def _set_setting(cur, key: str, value: str):
    cur.execute(
        """
        INSERT INTO platform_settings (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, value),
    )


# ── Endpoints: usuarios ────────────────────────────────────────────────────────

@router.get("/users")
def list_users(_: Annotated[str, Depends(require_admin)]) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                users = _load_users(cur)
        return {"users": [u.model_dump() for u in users]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] list_users error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener usuarios")


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserRequest,
    _:    Annotated[str, Depends(require_admin)],
) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_profile (user_id, display_name, role, status, timezone, preferences, updated_at)
                    VALUES (%s, %s, %s, 'active', 'America/Mexico_City', '{}', NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (body.email, body.display_name or body.email.split("@")[0], body.role),
                )
                if body.phone:
                    cur.execute(
                        """
                        INSERT INTO user_identities (canonical_user_id, identity_type, identity_value, created_at)
                        VALUES (%s, 'whatsapp', %s, NOW())
                        ON CONFLICT DO NOTHING
                        """,
                        (body.email, body.phone),
                    )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] create_user error: {exc}")
        raise HTTPException(status_code=500, detail="Error al crear usuario")


@router.patch("/users/{uid}")
def update_user(
    uid:  str,
    body: UpdateUserRequest,
    _:    Annotated[str, Depends(require_admin)],
) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                updates = []
                params  = []
                if body.display_name is not None:
                    updates.append("display_name = %s"); params.append(body.display_name)
                if body.role is not None:
                    updates.append("role = %s"); params.append(body.role)
                if body.status is not None:
                    updates.append("status = %s"); params.append(body.status)
                if not updates:
                    return {"status": "ok"}
                updates.append("updated_at = NOW()")
                params.append(uid)
                cur.execute(
                    f"UPDATE user_profile SET {', '.join(updates)} WHERE user_id = %s",
                    params,
                )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] update_user error: {exc}")
        raise HTTPException(status_code=500, detail="Error al actualizar usuario")


@router.delete("/users/{uid}", status_code=204)
def delete_user(
    uid: str,
    _:   Annotated[str, Depends(require_admin)],
) -> None:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_identities WHERE canonical_user_id = %s", (uid,))
                cur.execute("DELETE FROM user_profile WHERE user_id = %s", (uid,))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] delete_user error: {exc}")
        raise HTTPException(status_code=500, detail="Error al eliminar usuario")


@router.post("/users/{uid}/identity", status_code=201)
def add_identity(
    uid:  str,
    body: AddIdentityRequest,
    _:    Annotated[str, Depends(require_admin)],
) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_identities (canonical_user_id, identity_type, identity_value, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    (uid, body.identity_type, body.identity_value),
                )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] add_identity error: {exc}")
        raise HTTPException(status_code=500, detail="Error al añadir identidad")


@router.delete("/users/{uid}/identity/{value}", status_code=204)
def remove_identity(
    uid:   str,
    value: str,
    _:     Annotated[str, Depends(require_admin)],
) -> None:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_identities WHERE canonical_user_id = %s AND identity_value = %s",
                    (uid, value),
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] remove_identity error: {exc}")
        raise HTTPException(status_code=500, detail="Error al eliminar identidad")


# ── Endpoints: settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings_endpoint(_: Annotated[str, Depends(require_admin)]) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                allow = _get_setting(cur, "allow_access_requests", "false")
        return {"allow_access_requests": allow == "true"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] get_settings error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener configuración")


@router.patch("/settings")
def update_settings(
    body: SettingsUpdate,
    _:    Annotated[str, Depends(require_admin)],
) -> dict:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                if body.allow_access_requests is not None:
                    _set_setting(cur, "allow_access_requests",
                                 "true" if body.allow_access_requests else "false")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[admin] update_settings error: {exc}")
        raise HTTPException(status_code=500, detail="Error al actualizar configuración")
