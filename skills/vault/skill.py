"""
VaultSkill — lectura y escritura de secretos en HashiCorp Vault (KV v2).

Capacidades:
  get_secret(path)           — lee secret del KV v2
  put_secret(path, data)     — escribe/actualiza secret
  delete_secret(path)        — elimina secret
  has_secret(path)           — verifica si el secret existe
  list_secrets(path_prefix)  — lista claves bajo un prefijo

Autenticación: Kubernetes service account JWT (auth/kubernetes).
Mount point por defecto: "secret" (KV v2).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.vault")

_VAULT_ADDR  = os.environ.get("VAULT_ADDR", "http://vault.vault.svc.cluster.local:8200")
_VAULT_ROLE  = os.environ.get("VAULT_ROLE", "amael-productivity")
_VAULT_MOUNT = "secret"
_K8S_TOKEN   = "/var/run/secrets/kubernetes.io/serviceaccount/token"


# ── Inputs ────────────────────────────────────────────────────────────────────

class GetSecretInput(SkillInput):
    path: str
    mount: str = _VAULT_MOUNT

class PutSecretInput(SkillInput):
    path: str
    data: dict[str, Any]
    mount: str = _VAULT_MOUNT

class DeleteSecretInput(SkillInput):
    path: str
    mount: str = _VAULT_MOUNT

class HasSecretInput(SkillInput):
    path: str
    mount: str = _VAULT_MOUNT

class ListSecretsInput(SkillInput):
    path_prefix: str
    mount: str = _VAULT_MOUNT


# ── Client factory ────────────────────────────────────────────────────────────

def _vault_client():
    """Crea cliente HVAC autenticado via K8s SA JWT."""
    import hvac

    client = hvac.Client(url=_VAULT_ADDR)
    try:
        with open(_K8S_TOKEN) as f:
            jwt = f.read().strip()
        client.auth.kubernetes.login(role=_VAULT_ROLE, jwt=jwt)
    except FileNotFoundError:
        # Fuera de Kubernetes (dev/test) — intentar token de entorno
        token = os.environ.get("VAULT_TOKEN")
        if token:
            client.token = token
        else:
            raise RuntimeError(
                "No se encontró JWT de K8s ni VAULT_TOKEN en el entorno."
            )

    if not client.is_authenticated():
        raise RuntimeError(
            f"Vault auth falló (addr={_VAULT_ADDR}, role={_VAULT_ROLE})"
        )
    return client


# ── Skill ─────────────────────────────────────────────────────────────────────

class VaultSkill(BaseSkill):
    """
    Capacidad de acceso a secretos en HashiCorp Vault KV v2.
    Usada por ProductivityAgent (tokens Google OAuth) y SREAgent (credenciales).
    """

    name        = "vault"
    description = "Lectura/escritura de secretos en HashiCorp Vault KV v2"
    version     = "1.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        if isinstance(input, GetSecretInput):
            return await self.get_secret(input)
        if isinstance(input, PutSecretInput):
            return await self.put_secret(input)
        if isinstance(input, DeleteSecretInput):
            return await self.delete_secret(input)
        if isinstance(input, HasSecretInput):
            return await self.has_secret(input)
        if isinstance(input, ListSecretsInput):
            return await self.list_secrets(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por VaultSkill")

    async def get_secret(self, input: GetSecretInput) -> SkillOutput:
        """Lee un secret de Vault KV v2. Retorna el dict de datos."""
        try:
            client = _vault_client()
            resp   = client.secrets.kv.v2.read_secret_version(
                path=input.path, mount_point=input.mount
            )
            data = resp["data"]["data"]
            logger.info(f"[vault_skill] Secret leído: {input.path}")
            return SkillOutput.ok(data=data, path=input.path)
        except Exception as exc:
            if "InvalidPath" in type(exc).__name__ or "404" in str(exc):
                return SkillOutput.fail(
                    f"Secret no encontrado: {input.path}",
                    not_found=True,
                )
            logger.error(f"[vault_skill] get_secret error: {exc}")
            return SkillOutput.fail(str(exc))

    async def put_secret(self, input: PutSecretInput) -> SkillOutput:
        """Crea o actualiza un secret en Vault KV v2."""
        try:
            client = _vault_client()
            client.secrets.kv.v2.create_or_update_secret(
                path=input.path,
                secret=input.data,
                mount_point=input.mount,
            )
            logger.info(f"[vault_skill] Secret escrito: {input.path}")
            return SkillOutput.ok(data={"written": True}, path=input.path)
        except Exception as exc:
            logger.error(f"[vault_skill] put_secret error: {exc}")
            return SkillOutput.fail(str(exc))

    async def delete_secret(self, input: DeleteSecretInput) -> SkillOutput:
        """Elimina todas las versiones de un secret de Vault KV v2."""
        try:
            client = _vault_client()
            client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=input.path, mount_point=input.mount
            )
            logger.info(f"[vault_skill] Secret eliminado: {input.path}")
            return SkillOutput.ok(data={"deleted": True}, path=input.path)
        except Exception as exc:
            logger.error(f"[vault_skill] delete_secret error: {exc}")
            return SkillOutput.fail(str(exc))

    async def has_secret(self, input: HasSecretInput) -> SkillOutput:
        """Verifica si un secret existe sin leer su contenido."""
        try:
            client = _vault_client()
            client.secrets.kv.v2.read_secret_version(
                path=input.path, mount_point=input.mount
            )
            return SkillOutput.ok(data={"exists": True}, path=input.path)
        except Exception as exc:
            if "InvalidPath" in type(exc).__name__ or "404" in str(exc):
                return SkillOutput.ok(data={"exists": False}, path=input.path)
            logger.error(f"[vault_skill] has_secret error: {exc}")
            return SkillOutput.fail(str(exc))

    async def list_secrets(self, input: ListSecretsInput) -> SkillOutput:
        """Lista las claves bajo un prefijo en Vault KV v2."""
        try:
            client = _vault_client()
            resp   = client.secrets.kv.v2.list_secrets(
                path=input.path_prefix, mount_point=input.mount
            )
            keys = resp.get("data", {}).get("keys", [])
            return SkillOutput.ok(data=keys, count=len(keys), prefix=input.path_prefix)
        except Exception as exc:
            if "InvalidPath" in type(exc).__name__ or "404" in str(exc):
                return SkillOutput.ok(data=[], count=0, prefix=input.path_prefix)
            logger.error(f"[vault_skill] list_secrets error: {exc}")
            return SkillOutput.fail(str(exc))

    async def health_check(self) -> bool:
        """Verifica que Vault está unsealed y responde."""
        try:
            import requests as _req
            resp = _req.get(f"{_VAULT_ADDR}/v1/sys/health", timeout=5)
            # 200 = initialized, unsealed, active
            # 429 = standby (still healthy for reads)
            return resp.status_code in (200, 429)
        except Exception as exc:
            logger.warning(f"[vault_skill] health_check falló: {exc}")
            return False
