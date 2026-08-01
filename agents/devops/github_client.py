"""
GitHub REST API v3 client — reemplazo de `bitbucket_client` tras retirar la POC.

Expone exactamente las mismas funciones y firmas que `bitbucket_client` para que
`agents/devops/agent.py` no necesite reescribirse. El primer parámetro sigue
llamándose `workspace` aunque en GitHub sea el *owner* de la organización o
usuario — se conserva el nombre para no romper el contrato.

Diferencias reales con Bitbucket, que sí obligan a lógica distinta:

  - Actualizar un archivo requiere el `sha` del blob **en esa rama**. Bitbucket
    no lo pide. `commit_file()` lo resuelve solo: consulta el archivo antes y,
    si no existe (404), lo crea.
  - GitHub no deja aprobar tu propio PR. `approve_pr()` lo reporta explícito
    en vez de fallar con un 422 opaco.
  - Los Pipelines de Bitbucket se mapean a GitHub Actions: `trigger_pipeline`
    → workflow_dispatch, `get_pipeline`/`list_pipelines` → actions/runs.

Auth: GITHUB_TOKEN (PAT con scopes `repo` y `workflow`).
Base URL: https://api.github.com
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("agents.camael.github")

_GH_BASE = "https://api.github.com"
_TIMEOUT = 30.0


def _token() -> str:
    t = os.environ.get("GITHUB_TOKEN", "")
    if not t:
        raise RuntimeError("GITHUB_TOKEN no configurado en el entorno")
    return t


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_token()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"GitHub API {resp.status_code} en {resp.url}: {detail}")


async def gh_get(path: str, params: dict | None = None) -> dict:
    url = f"{_GH_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(), params=params or {})
        _raise(resp)
        return resp.json()


async def gh_post(path: str, body: dict) -> dict:
    url = f"{_GH_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_headers(), json=body)
        _raise(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


async def gh_put(path: str, body: dict) -> dict:
    url = f"{_GH_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(url, headers=_headers(), json=body)
        _raise(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


# ── Operaciones de rama ───────────────────────────────────────────────────────

async def get_branch_head(workspace: str, repo: str, branch: str = "main") -> str:
    """Retorna el SHA del último commit de la rama."""
    data = await gh_get(f"repos/{workspace}/{repo}/git/ref/heads/{branch}")
    return data["object"]["sha"]


async def create_branch(
    workspace: str,
    repo: str,
    branch_name: str,
    from_branch: str = "main",
) -> dict:
    """Crea una nueva rama desde from_branch."""
    head_sha = await get_branch_head(workspace, repo, from_branch)
    return await gh_post(
        f"repos/{workspace}/{repo}/git/refs",
        {"ref": f"refs/heads/{branch_name}", "sha": head_sha},
    )


# ── Operaciones de archivos ───────────────────────────────────────────────────

async def _get_file_meta(
    workspace: str, repo: str, file_path: str, ref: str
) -> dict | None:
    """Metadata del archivo (incluye `sha` y `content` base64). None si no existe."""
    url = f"{_GH_BASE}/repos/{workspace}/{repo}/contents/{file_path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(), params={"ref": ref})
        if resp.status_code == 404:
            return None
        _raise(resp)
        return resp.json()


async def read_file(
    workspace: str,
    repo: str,
    file_path: str,
    ref: str = "main",
) -> str:
    """Lee el contenido de un archivo en el repo."""
    meta = await _get_file_meta(workspace, repo, file_path, ref)
    if meta is None:
        raise RuntimeError(
            f"GitHub API 404: {file_path} no existe en {workspace}/{repo}@{ref}"
        )
    if meta.get("encoding") != "base64" or "content" not in meta:
        raise RuntimeError(
            f"Respuesta inesperada leyendo {file_path}: encoding={meta.get('encoding')}"
        )
    return base64.b64decode(meta["content"]).decode("utf-8")


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

    A diferencia de Bitbucket, GitHub exige el `sha` del blob actual para
    actualizar. Se consulta primero contra la rama destino; si el archivo no
    existe se omite el sha y GitHub lo crea.

    `author` acepta el formato "Nombre <email>" que usaba Bitbucket.
    """
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }

    existing = await _get_file_meta(workspace, repo, file_path, branch)
    if existing and existing.get("sha"):
        body["sha"] = existing["sha"]

    if author:
        match = re.match(r"^\s*(.+?)\s*<(.+?)>\s*$", author)
        if match:
            body["committer"] = {"name": match.group(1), "email": match.group(2)}
        else:
            logger.warning(f"[gh] author en formato no reconocido, se ignora: {author!r}")

    return await gh_put(f"repos/{workspace}/{repo}/contents/{file_path}", body)


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
    """
    Crea un Pull Request.

    El dict devuelto incluye las claves nativas de GitHub más `id` y
    `links.html.href` para mantener la forma que ya consume agent.py.
    """
    pr = await gh_post(
        f"repos/{workspace}/{repo}/pulls",
        {
            "title": title,
            "body": description,
            "head": source_branch,
            "base": dest_branch,
        },
    )

    if reviewers:
        try:
            await gh_post(
                f"repos/{workspace}/{repo}/pulls/{pr['number']}/requested_reviewers",
                {"reviewers": reviewers},
            )
        except Exception as exc:
            # Un reviewer inválido no debe tumbar un PR ya creado.
            logger.warning(f"[gh] No se pudieron asignar reviewers {reviewers}: {exc}")

    return _pr_compat(pr)


def _pr_compat(pr: dict) -> dict:
    """Añade las claves `id` y `links.html.href` que espera agent.py."""
    out = dict(pr)
    out["id"] = pr.get("number", 0)
    out["links"] = {"html": {"href": pr.get("html_url", "")}}
    return out


async def approve_pr(workspace: str, repo: str, pr_id: int) -> dict:
    """
    Aprueba un PR.

    GitHub rechaza que el autor apruebe su propio PR (422). Como Camael crea y
    aprueba con el mismo token, esto falla siempre salvo que se use un token
    distinto — se reporta claro en vez de dejar un 422 sin contexto.
    """
    try:
        return await gh_post(
            f"repos/{workspace}/{repo}/pulls/{pr_id}/reviews",
            {"event": "APPROVE"},
        )
    except RuntimeError as exc:
        if "422" in str(exc):
            raise RuntimeError(
                f"GitHub no permite aprobar el PR #{pr_id} con el mismo token que lo "
                f"creó. Se requiere un token de otro usuario, o mergear sin aprobación "
                f"previa si la protección de rama lo permite. Detalle: {exc}"
            ) from exc
        raise


async def merge_pr(
    workspace: str,
    repo: str,
    pr_id: int,
    message: str = "",
    merge_strategy: str = "merge_commit",
) -> dict:
    """
    Mergea un PR.

    `merge_strategy` conserva la nomenclatura de Bitbucket y se traduce a los
    merge_method de GitHub. El resultado incluye `merge_commit.hash` para
    mantener la forma que consume agent.py.
    """
    method = {
        "merge_commit": "merge",
        "squash":       "squash",
        "fast_forward": "rebase",
    }.get(merge_strategy, "merge")

    body: dict[str, Any] = {"merge_method": method}
    if message:
        body["commit_message"] = message

    result = await gh_put(f"repos/{workspace}/{repo}/pulls/{pr_id}/merge", body)
    out = dict(result)
    out["merge_commit"] = {"hash": result.get("sha", "")}
    return out


async def get_pr(workspace: str, repo: str, pr_id: int) -> dict:
    """Estado actual de un PR."""
    return _pr_compat(await gh_get(f"repos/{workspace}/{repo}/pulls/{pr_id}"))


async def close_pr(workspace: str, repo: str, pr_id: int) -> int:
    """
    Cierra (declina) un PR sin mergear.

    Retorna el código HTTP para que quien llama distinga "ya no existe / ya
    estaba cerrado" (404/409) de un fallo real, igual que hacía el flujo de
    Bitbucket.
    """
    url = f"{_GH_BASE}/repos/{workspace}/{repo}/pulls/{pr_id}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.patch(url, headers=_headers(), json={"state": "closed"})
        return resp.status_code


# ── GitHub Actions (equivalente a los Pipelines de Bitbucket) ─────────────────

async def trigger_pipeline(
    workspace: str,
    repo: str,
    branch: str = "main",
    pipeline_selector: str | None = None,
    variables: list[dict] | None = None,
) -> dict:
    """
    Dispara un workflow de GitHub Actions vía workflow_dispatch.

    `pipeline_selector` es el nombre del archivo del workflow (p. ej.
    "deploy.yml"). Si no se indica, se usa el primer workflow activo del repo.
    `variables` acepta la forma de Bitbucket [{"key": k, "value": v}] y se
    traduce a los `inputs` de Actions.

    Nota: workflow_dispatch devuelve 204 sin cuerpo, así que no hay un id de
    ejecución que retornar de inmediato — se informa del disparo.
    """
    workflow = pipeline_selector
    if not workflow:
        data = await gh_get(f"repos/{workspace}/{repo}/actions/workflows")
        active = [w for w in data.get("workflows", []) if w.get("state") == "active"]
        if not active:
            raise RuntimeError(f"No hay workflows activos en {workspace}/{repo}")
        workflow = active[0]["path"].split("/")[-1]

    body: dict[str, Any] = {"ref": branch}
    if variables:
        body["inputs"] = {
            v["key"]: str(v["value"]) for v in variables if "key" in v and "value" in v
        }

    await gh_post(
        f"repos/{workspace}/{repo}/actions/workflows/{workflow}/dispatches", body
    )
    return {"workflow": workflow, "ref": branch, "dispatched": True}


async def get_pipeline(workspace: str, repo: str, pipeline_uuid: str) -> dict:
    """Estado de una ejecución de Actions por run_id."""
    return await gh_get(f"repos/{workspace}/{repo}/actions/runs/{pipeline_uuid}")


async def list_pipelines(
    workspace: str,
    repo: str,
    limit: int = 5,
) -> list[dict]:
    """Lista las ejecuciones de Actions más recientes, en la forma de Bitbucket."""
    data = await gh_get(
        f"repos/{workspace}/{repo}/actions/runs", params={"per_page": limit}
    )
    runs = []
    for r in data.get("workflow_runs", []):
        runs.append({
            "uuid":         str(r.get("id", "")),
            "build_number": r.get("run_number", ""),
            "status":       (r.get("status") or "UNKNOWN").upper(),
            "result":       (r.get("conclusion") or "").upper(),
            "branch":       r.get("head_branch", ""),
            "created_on":   r.get("created_at", ""),
        })
    return runs


# ── Code Search ───────────────────────────────────────────────────────────────

_DISCOVERY_MAX_FILES = int(os.environ.get("SCM_DISCOVERY_MAX_FILES", "60"))


async def _list_yaml_paths(workspace: str, repo: str, branch: str) -> list[str]:
    """Todas las rutas .yaml/.yml del repo, en una sola llamada al árbol de git."""
    data = await gh_get(
        f"repos/{workspace}/{repo}/git/trees/{branch}", params={"recursive": "1"}
    )
    if data.get("truncated"):
        logger.warning(
            f"[gh] El árbol de {workspace}/{repo} viene truncado — "
            f"el discovery puede no ver todos los manifests."
        )
    return [
        t["path"]
        for t in data.get("tree", [])
        if t.get("type") == "blob" and t.get("path", "").endswith((".yaml", ".yml"))
    ]


async def search_file_in_repo(
    workspace: str,
    repo: str,
    resource_name: str,
    branch: str = "main",
) -> str | None:
    """
    Busca el YAML que **define** el deployment 'resource_name'.

    No usa la Code Search API de GitHub: los repos privados no siempre están
    indexados y devuelve 0 resultados sin error, lo que haría fallar el
    discovery en silencio. En su lugar lista el árbol de git (una llamada) y
    confirma leyendo el contenido, que es determinista.

    Orden de búsqueda:
      1. Candidatos cuya ruta menciona el recurso — se leen primero.
      2. Se confirma que el YAML declara `name: <resource_name>`.
      3. Si ningún contenido coincide, se acepta la coincidencia por ruta.
    """
    # Kubernetes pod names are RFC 1123 DNS labels: [a-z0-9-]
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,252}", resource_name):
        logger.warning(f"[gh] resource_name rechazado (formato inválido): '{resource_name}'")
        return None

    try:
        paths = await _list_yaml_paths(workspace, repo, branch)
        if not paths:
            logger.info(f"[gh] Discovery: {workspace}/{repo} no tiene YAMLs en {branch}")
            return None

        # Los que mencionan el recurso en la ruta van primero; el resto después.
        by_path = [p for p in paths if resource_name in p]
        others  = [p for p in paths if p not in by_path]
        ordered = (by_path + others)[:_DISCOVERY_MAX_FILES]

        needle = re.compile(rf"^\s*name:\s*{re.escape(resource_name)}\s*$", re.MULTILINE)

        for path in ordered:
            try:
                content = await read_file(workspace, repo, path, branch)
            except Exception:
                continue
            if needle.search(content):
                logger.info(f"[gh] Discovery: '{resource_name}' → {path} (por contenido)")
                return path

        if by_path:
            logger.info(
                f"[gh] Discovery: '{resource_name}' → {by_path[0]} "
                f"(por ruta; ningún YAML lo declara explícitamente)"
            )
            return by_path[0]

        logger.info(f"[gh] Discovery: no se encontró YAML para '{resource_name}'")
        return None

    except Exception as exc:
        logger.warning(f"[gh] search_file_in_repo error (no crítico): {exc}")
        return None
