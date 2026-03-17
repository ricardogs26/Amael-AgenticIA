"""
Router /api/memory/profile — perfil del usuario actual.

Endpoints:
  GET  /api/memory/profile — obtiene el perfil del usuario autenticado
  POST /api/memory/profile — actualiza el perfil del usuario
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.profile")

router = APIRouter(prefix="/api/memory", tags=["profile"])


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    timezone:     str | None = None
    preferences:  dict[str, Any] | None = None


def _get_or_create_profile(cur, user_id: str) -> dict:
    """Obtiene el perfil, creando uno vacío si no existe."""
    cur.execute(
        "SELECT user_id, display_name, timezone, preferences, role, status "
        "FROM user_profile WHERE user_id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        return {
            "user_id":      row[0],
            "display_name": row[1],
            "timezone":     row[2] or "America/Mexico_City",
            "preferences":  row[3] or {},
            "role":         row[4] or "user",
            "status":       row[5] or "active",
        }
    # Auto-crear perfil en primer login
    cur.execute(
        """
        INSERT INTO user_profile (user_id, display_name, timezone, preferences, role, status, updated_at)
        VALUES (%s, %s, 'America/Mexico_City', '{}', 'user', 'active', NOW())
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, user_id.split("@")[0]),
    )
    return {
        "user_id":      user_id,
        "display_name": user_id.split("@")[0],
        "timezone":     "America/Mexico_City",
        "preferences":  {},
        "role":         "user",
        "status":       "active",
    }


@router.get("/profile")
def get_profile(user_id: Annotated[str, Depends(get_current_user)]) -> dict:
    """Retorna el perfil del usuario autenticado."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                profile = _get_or_create_profile(cur, user_id)
        return {"profile": profile}
    except Exception as exc:
        logger.error(f"[profile] get error: {exc}")
        raise HTTPException(status_code=500, detail="Error al obtener perfil")


@router.post("/profile")
def update_profile(
    body:    ProfileUpdate,
    user_id: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Actualiza el perfil del usuario autenticado."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Obtener valores actuales
                profile = _get_or_create_profile(cur, user_id)

                new_display = body.display_name if body.display_name is not None else profile["display_name"]
                new_tz      = body.timezone     if body.timezone     is not None else profile["timezone"]
                new_prefs   = body.preferences  if body.preferences  is not None else profile["preferences"]

                import json
                cur.execute(
                    """
                    UPDATE user_profile
                    SET display_name = %s, timezone = %s, preferences = %s, updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (new_display, new_tz, json.dumps(new_prefs), user_id),
                )
        return {"status": "ok"}
    except Exception as exc:
        logger.error(f"[profile] update error: {exc}")
        raise HTTPException(status_code=500, detail="Error al actualizar perfil")
