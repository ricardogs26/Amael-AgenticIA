"""
Router /api/auth — Google OAuth 2.0 + JWT.

Flujo principal:
  GET /api/auth/login    → redirige a Google consent screen
  GET /api/auth/callback → recibe código, crea JWT, redirige al frontend

Flujo Calendar (opcional):
  GET /api/auth/calendar          → inicia OAuth con scope Calendar+Gmail
  GET /api/auth/calendar/callback → guarda refresh_token en Vault via productivity-service
  GET /api/auth/calendar/status   → verifica si el usuario tiene Calendar conectado
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from jose import jwt

from config.settings import settings
from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.auth_router")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── OAuth client (lazy singleton) ─────────────────────────────────────────────

_oauth = None

def _get_oauth():
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth
        _oauth = OAuth()
        _oauth.register(
            name="google",
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth.register(
            name="google_calendar",
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            client_kwargs={
                "scope": (
                    "openid email "
                    "https://www.googleapis.com/auth/calendar "
                    "https://www.googleapis.com/auth/gmail.readonly"
                )
            },
        )
    return _oauth


_FRONTEND_URL = "https://amael-ia.richardx.dev"
_CALLBACK_URL = f"{_FRONTEND_URL}/api/auth/callback"
_CALENDAR_CALLBACK_URL = f"{_FRONTEND_URL}/api/auth/calendar/callback"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_jwt(email: str) -> str:
    return jwt.encode(
        {"sub": email},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


# ── Endpoints principales ──────────────────────────────────────────────────────

@router.get("/login")
async def login(request: Request):
    """Inicia el flujo OAuth con Google."""
    oauth = _get_oauth()
    return await oauth.google.authorize_redirect(request, _CALLBACK_URL)


def _is_user_active(email: str) -> bool:
    """Verifica que el email existe en user_profile con status='active'."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_profile WHERE user_id = %s AND status = 'active'",
                    (email,),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        logger.error(f"[auth] DB check failed para {email!r}: {exc}")
        return False


@router.get("/callback")
async def auth_callback(request: Request):
    """
    Recibe el código de Google, valida el email contra user_profile (DB),
    crea el JWT y redirige al frontend con ?token=&name=&picture=
    """
    oauth = _get_oauth()
    try:
        token     = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo") or {}
        email     = user_info.get("email", "")

        if not email or not _is_user_active(email):
            logger.warning(f"[auth] Login rechazado para: {email!r}")
            return Response(
                status_code=302,
                headers={"location": f"{_FRONTEND_URL}?error=unauthorized"},
            )

        jwt_token = _create_jwt(email)
        params = {
            "token":   jwt_token,
            "name":    user_info.get("name", "Usuario"),
            "picture": user_info.get("picture", ""),
        }
        redirect_url = f"{_FRONTEND_URL}?{urlencode(params)}"
        logger.info(f"[auth] Login exitoso: {email}")
        return Response(status_code=302, headers={"location": redirect_url})

    except Exception as exc:
        logger.error(f"[auth] Error en callback: {exc}", exc_info=True)
        return Response(
            status_code=302,
            headers={"location": f"{_FRONTEND_URL}?error=auth_failed"},
        )


# ── Calendar OAuth ─────────────────────────────────────────────────────────────

@router.get("/calendar")
async def calendar_auth(request: Request, token: str | None = None):
    """Inicia OAuth de Google Calendar. Requiere JWT como query param ?token="""
    if not token:
        raise HTTPException(status_code=401, detail="Token requerido")
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        email = payload.get("sub") or payload.get("email")
        if not email or not _is_user_active(email):
            raise HTTPException(status_code=403, detail="Usuario no autorizado")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")

    request.session["calendar_user"] = email
    oauth = _get_oauth()
    return await oauth.google_calendar.authorize_redirect(
        request,
        _CALENDAR_CALLBACK_URL,
        access_type="offline",
        prompt="consent",
    )


@router.get("/calendar/callback")
async def calendar_callback(request: Request):
    """Recibe el refresh_token de Calendar y lo guarda en Vault via productivity-service."""
    oauth = _get_oauth()
    try:
        token      = await oauth.google_calendar.authorize_access_token(request)
        user_email = request.session.get("calendar_user") or token.get("userinfo", {}).get("email", "")

        if not user_email:
            return Response(
                status_code=302,
                headers={"location": f"{_FRONTEND_URL}?calendar_error=1"},
            )

        refresh_token = token.get("refresh_token")
        if not refresh_token:
            logger.error(f"[auth] Calendar: Google no devolvió refresh_token para {user_email}")
            return Response(
                status_code=302,
                headers={"location": f"{_FRONTEND_URL}?calendar_error=1"},
            )

        # Guardar en Vault via productivity-service
        import httpx
        payload = {
            "user_email":     user_email,
            "token":          token.get("access_token"),
            "refresh_token":  refresh_token,
            "token_uri":      "https://oauth2.googleapis.com/token",
            "client_id":      settings.google_client_id,
            "client_secret":  settings.google_client_secret,
            "scopes": [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
        }
        r = httpx.post(
            f"{settings.productivity_service_url}/credentials",
            json=payload,
            headers={"Authorization": f"Bearer {settings.internal_api_secret}"},
            timeout=30.0,
        )
        if r.status_code != 200:
            logger.error(f"[auth] Error guardando credenciales Calendar: {r.text}")
            return Response(
                status_code=302,
                headers={"location": f"{_FRONTEND_URL}?calendar_error=1"},
            )

        logger.info(f"[auth] Calendar conectado para {user_email}")
        return Response(
            status_code=302,
            headers={"location": f"{_FRONTEND_URL}?calendar_connected=1"},
        )

    except Exception as exc:
        logger.error(f"[auth] Error en calendar callback: {exc}", exc_info=True)
        return Response(
            status_code=302,
            headers={"location": f"{_FRONTEND_URL}?calendar_error=1"},
        )


@router.get("/calendar/status")
def calendar_status(user_id: str = Depends(get_current_user)) -> dict:
    """Verifica si el usuario tiene Google Calendar conectado."""
    import httpx
    try:
        r = httpx.get(
            f"{settings.productivity_service_url}/credentials/status",
            params={"user_email": user_id},
            headers={"Authorization": f"Bearer {settings.internal_api_secret}"},
            timeout=10.0,
        )
        return r.json() if r.status_code == 200 else {"connected": False}
    except Exception:
        return {"connected": False}
