"""
Bitbucket API v2.0 client — autenticación via API Token (Bearer).

Operaciones soportadas:
  get_branch_head()   — SHA del último commit de una rama
  create_branch()     — crea nueva rama desde una existente
  read_file()         — lee el contenido de un archivo en el repo
  commit_file()       — crea o actualiza un archivo via multipart POST
  create_pr()         — crea Pull Request
  approve_pr()        — aprueba un Pull Request
  merge_pr()          — mergea un Pull Request aprobado
  trigger_pipeline()  — dispara un Bitbucket Pipeline
  get_pipeline()      — estado de un pipeline por UUID
  list_pipelines()    — lista pipelines recientes

Auth: BITBUCKET_TOKEN (API token) + BITBUCKET_USERNAME del secret k8s.
Base URL: https://api.bitbucket.org/2.0
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("agents.camael.bitbucket")

_BB_BASE = "https://api.bitbucket.org/2.0"
_TIMEOUT = 30.0


def _token() -> str:
    t = os.environ.get("BITBUCKET_TOKEN", "")
    if not t:
        raise RuntimeError("BITBUCKET_TOKEN no configurado en el entorno")
    return t


def _username() -> str:
    """
    Username para Basic auth.
    Bitbucket acepta email o username de cuenta como usuario para Basic auth.
    Orden de preferencia: BITBUCKET_USERNAME → BITBUCKET_EMAIL → error.
    """
    u = os.environ.get("BITBUCKET_USERNAME", "")
    if not u:
        raise RuntimeError("BITBUCKET_USERNAME no configurado en el entorno")
    return u


def _auth() -> tuple[str, str]:
    """Retorna (username, token) para httpx auth=() (Basic auth)."""
    return (_username(), _token())


def _headers() -> dict[str, str]:
    return {"Accept": "application/json"}


# ── GET helper ────────────────────────────────────────────────────────────────

async def bb_get(path: str, params: dict | None = None) -> dict:
    url = f"{_BB_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=_auth()) as client:
        resp = await client.get(url, headers=_headers(), params=params or {})
        _raise(resp)
        return resp.json()


# ── POST helper ───────────────────────────────────────────────────────────────

async def bb_post(path: str, body: dict) -> dict:
    url = f"{_BB_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=_auth()) as client:
        resp = await client.post(url, headers=_headers(), json=body)
        _raise(resp)
        # Algunos endpoints retornan 200, otros 201, otros 204
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


# ── Multipart POST (commit de archivos) ───────────────────────────────────────

async def bb_multipart(path: str, fields: dict[str, str]) -> dict:
    """
    Sube un archivo a Bitbucket via multipart/form-data.
    El campo cuyo nombre es la ruta del archivo contiene su contenido.
    """
    url = f"{_BB_BASE}/{path.lstrip('/')}"
    files = [(k, (None, v)) for k, v in fields.items()]
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=_auth()) as client:
        resp = await client.post(url, headers=_headers(), files=files)
        _raise(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(
            f"Bitbucket API {resp.status_code} en {resp.url}: {detail}"
        )


# ── Operaciones de rama ───────────────────────────────────────────────────────

async def get_branch_head(workspace: str, repo: str, branch: str = "main") -> str:
    """Retorna el SHA del último commit de la rama."""
    data = await bb_get(
        f"repositories/{workspace}/{repo}/refs/branches/{branch}"
    )
    return data["target"]["hash"]


async def create_branch(
    workspace: str,
    repo: str,
    branch_name: str,
    from_branch: str = "main",
) -> dict:
    """Crea una nueva rama desde from_branch."""
    head_sha = await get_branch_head(workspace, repo, from_branch)
    return await bb_post(
        f"repositories/{workspace}/{repo}/refs/branches",
        {
            "name": branch_name,
            "target": {"hash": head_sha},
        },
    )


# ── Operaciones de archivos ───────────────────────────────────────────────────

async def read_file(
    workspace: str,
    repo: str,
    file_path: str,
    ref: str = "main",
) -> str:
    """Lee el contenido de un archivo en el repo."""
    url = f"{_BB_BASE}/repositories/{workspace}/{repo}/src/{ref}/{file_path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, auth=_auth()) as client:
        resp = await client.get(url, headers=_headers())
        _raise(resp)
        return resp.text


async def commit_file(
    workspace: str,
    repo: str,
    file_path: str,
    content: str,
    branch: str,
    message: str,
    author: str | None = None,
) -> dict:
    """
    Crea o actualiza un archivo en el repo.
    Usa POST /repositories/{ws}/{repo}/src (multipart).
    El nombre del campo es la ruta del archivo.
    """
    fields: dict[str, str] = {
        file_path: content,
        "message": message,
        "branch": branch,
    }
    if author:
        fields["author"] = author
    return await bb_multipart(
        f"repositories/{workspace}/{repo}/src",
        fields,
    )


# ── Pull Requests ─────────────────────────────────────────────────────────────

async def create_pr(
    workspace: str,
    repo: str,
    title: str,
    description: str,
    source_branch: str,
    dest_branch: str = "main",
    reviewers: list[str] | None = None,
) -> dict:
    """Crea un Pull Request y retorna el objeto PR (incluye 'id' y 'links')."""
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": dest_branch}},
        "close_source_branch": True,
    }
    if reviewers:
        body["reviewers"] = [{"uuid": r} for r in reviewers]
    return await bb_post(
        f"repositories/{workspace}/{repo}/pullrequests",
        body,
    )


async def approve_pr(workspace: str, repo: str, pr_id: int) -> dict:
    """Aprueba un PR. Requiere que el token sea de un usuario diferente al autor."""
    return await bb_post(
        f"repositories/{workspace}/{repo}/pullrequests/{pr_id}/approve",
        {},
    )


async def merge_pr(
    workspace: str,
    repo: str,
    pr_id: int,
    message: str = "",
    merge_strategy: str = "merge_commit",
) -> dict:
    """
    Mergea un PR aprobado.
    merge_strategy: merge_commit | squash | fast_forward
    """
    body: dict[str, Any] = {"merge_strategy": merge_strategy}
    if message:
        body["message"] = message
    return await bb_post(
        f"repositories/{workspace}/{repo}/pullrequests/{pr_id}/merge",
        body,
    )


async def get_pr(workspace: str, repo: str, pr_id: int) -> dict:
    """Estado actual de un PR."""
    return await bb_get(
        f"repositories/{workspace}/{repo}/pullrequests/{pr_id}"
    )


# ── Pipelines ─────────────────────────────────────────────────────────────────

async def trigger_pipeline(
    workspace: str,
    repo: str,
    branch: str = "main",
    pipeline_selector: str | None = None,
    variables: list[dict] | None = None,
) -> dict:
    """
    Dispara un Bitbucket Pipeline.

    pipeline_selector: nombre del pipeline custom (en bitbucket-pipelines.yml)
    Si es None dispara el pipeline default de la rama.
    """
    target: dict[str, Any] = {
        "ref_type": "branch",
        "type": "pipeline_ref_target",
        "ref_name": branch,
    }
    if pipeline_selector:
        target["selector"] = {"type": "custom", "pattern": pipeline_selector}

    body: dict[str, Any] = {"target": target}
    if variables:
        body["variables"] = variables

    return await bb_post(
        f"repositories/{workspace}/{repo}/pipelines/",
        body,
    )


async def get_pipeline(workspace: str, repo: str, pipeline_uuid: str) -> dict:
    """Estado de un pipeline por UUID."""
    return await bb_get(
        f"repositories/{workspace}/{repo}/pipelines/{pipeline_uuid}"
    )


async def list_pipelines(
    workspace: str,
    repo: str,
    limit: int = 5,
) -> list[dict]:
    """Lista los pipelines más recientes del repo."""
    data = await bb_get(
        f"repositories/{workspace}/{repo}/pipelines/",
        params={"pagelen": limit, "sort": "-created_on"},
    )
    pipelines = []
    for p in data.get("values", []):
        state = p.get("state", {})
        pipelines.append({
            "uuid":       p.get("uuid", ""),
            "build_number": p.get("build_number", ""),
            "status":     state.get("name", "UNKNOWN"),
            "result":     (state.get("result") or {}).get("name", ""),
            "branch":     (p.get("target") or {}).get("ref_name", ""),
            "created_on": p.get("created_on", ""),
        })
    return pipelines


# ── Code Search ───────────────────────────────────────────────────────────────

async def search_file_in_repo(
    workspace: str,
    repo: str,
    resource_name: str,
) -> str | None:
    """
    Busca el archivo YAML que define el deployment 'resource_name' en el repo.

    Usa Bitbucket Code Search API v2.0.
    Filtra solo archivos .yaml / .yml.
    Retorna la ruta del primer match o None si no encuentra.

    Ejemplo: search_file_in_repo("amael_agenticia", "amael-agentic-backend", "amael-demo-oom")
             → "k8s/demo/amael-demo-oom.yaml"
    """
    # Kubernetes pod names are RFC 1123 DNS labels: [a-z0-9-]
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,252}", resource_name):
        logger.warning(f"[bb] resource_name rejected (invalid format): '{resource_name}'")
        return None

    url = f"{_BB_BASE}/repositories/{workspace}/{repo}/search/code"
    query = f"name: {resource_name}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, auth=_auth()) as client:
            resp = await client.get(
                url,
                headers=_headers(),
                params={"search_query": query, "pagelen": 10},
            )
            if resp.status_code == 404:
                logger.warning(f"[bb] Code search no disponible para {workspace}/{repo}")
                return None
            _raise(resp)
            data = resp.json()

        for result in data.get("values", []):
            path = result.get("file", {}).get("path", "")
            if path.endswith(".yaml") or path.endswith(".yml"):
                logger.info(f"[bb] Discovery: '{resource_name}' → {path}")
                return path

        logger.info(f"[bb] Discovery: no se encontró YAML para '{resource_name}'")
        return None

    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning(f"[bb] search_file_in_repo error (no crítico): {exc}")
        return None
