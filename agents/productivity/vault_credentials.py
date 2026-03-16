"""
Vault Credentials — gestión de tokens OAuth de Google en HashiCorp Vault.

Migrado desde productivity-service/app/services/auth_service.py:
  get_user_credentials()  — lee tokens de Vault KV v2
  save_user_credentials() — escribe tokens a Vault KV v2
  get_auth_flow()         — construye el flujo OAuth para Google Calendar/Gmail

Autenticación a Vault: Kubernetes service account JWT (auth/kubernetes).
Path KV v2: secret/data/amael/google-tokens/{sanitized_email}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("agents.productivity.vault")

_K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_VAULT_MOUNT    = "secret"
_VAULT_BASE_PATH = "amael/google-tokens"

_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _sanitize_email(email: str) -> str:
    """Convierte email a segmento de path válido para Vault."""
    return email.replace("@", "_at_").replace(".", "_dot_")


def _vault_client():
    """
    Crea un cliente HVAC autenticado via Kubernetes service account JWT.
    Requiere VAULT_ADDR y VAULT_ROLE en el entorno.
    """
    import hvac
    from config.settings import get_settings
    settings = get_settings()

    vault_addr = str(settings.vault_addr)
    vault_role = os.environ.get("VAULT_ROLE", "amael-productivity")

    client = hvac.Client(url=vault_addr)
    with open(_K8S_TOKEN_PATH) as f:
        jwt = f.read().strip()

    client.auth.kubernetes.login(role=vault_role, jwt=jwt)
    if not client.is_authenticated():
        raise RuntimeError(
            f"Vault auth falló (addr={vault_addr}, role={vault_role})"
        )
    return client


def get_user_credentials(user_email: str) -> Optional[object]:
    """
    Recupera las credenciales OAuth de Google desde Vault KV v2.
    Retorna None si el usuario no tiene credenciales o hubo un error.
    """
    try:
        import hvac
        from google.oauth2.credentials import Credentials

        client = _vault_client()
        path   = f"{_VAULT_BASE_PATH}/{_sanitize_email(user_email)}"
        resp   = client.secrets.kv.v2.read_secret_version(
            path=path, mount_point=_VAULT_MOUNT
        )
        creds_data = resp["data"]["data"]
        creds      = Credentials.from_authorized_user_info(creds_data)
        logger.info(f"[vault_creds] Credenciales cargadas para {user_email}")
        return creds
    except Exception as exc:
        # hvac.exceptions.InvalidPath → primera vez del usuario
        if "InvalidPath" in type(exc).__name__ or "404" in str(exc):
            logger.info(f"[vault_creds] Sin credenciales para {user_email} (primera vez)")
        else:
            logger.warning(f"[vault_creds] Error obteniendo credenciales de {user_email}: {exc}")
        return None


def save_user_credentials(user_email: str, creds) -> None:
    """
    Persiste las credenciales OAuth de Google en Vault KV v2 (cifradas en reposo).
    Path: secret/data/amael/google-tokens/{sanitized_email}
    """
    try:
        client     = _vault_client()
        creds_dict = json.loads(creds.to_json())
        path       = f"{_VAULT_BASE_PATH}/{_sanitize_email(user_email)}"
        client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret=creds_dict,
            mount_point=_VAULT_MOUNT,
        )
        logger.info(f"[vault_creds] Credenciales guardadas para {user_email}")
    except Exception as exc:
        logger.error(f"[vault_creds] Error guardando credenciales de {user_email}: {exc}")
        raise


def has_credentials(user_email: str) -> bool:
    """Verifica si el usuario tiene credenciales válidas en Vault."""
    creds = get_user_credentials(user_email)
    if creds is None:
        return False
    return getattr(creds, "valid", False) or bool(getattr(creds, "refresh_token", None))


def get_auth_flow():
    """
    Construye el flujo OAuth de Google Calendar/Gmail con los parámetros del entorno.
    Migrado desde productivity-service/app/services/auth_service.py → get_auth_flow()
    """
    from google_auth_oauthlib.flow import Flow
    from config.settings import get_settings
    settings = get_settings()

    return Flow.from_client_config(
        client_config={
            "web": {
                "client_id":     settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_GOOGLE_SCOPES,
        redirect_uri=os.environ.get(
            "GOOGLE_REDIRECT_URI",
            "https://amael-ia.richardx.dev/api/auth/calendar/callback",
        ),
    )
