"""
GitHubTool — integración con la GitHub API v3/v4.

Capacidades de lectura:
  get_repo(owner, repo)                      — metadata del repositorio
  list_issues(owner, repo, state, labels)    — issues filtrados
  create_issue(owner, repo, title, body)     — crea un issue
  list_pull_requests(owner, repo, state)     — pull requests
  get_workflow_runs(owner, repo, workflow)   — últimas ejecuciones CI

Capacidades de escritura (Phase 1 — Gabriel):
  get_file_contents(owner, repo, path, ref)  — lee un archivo del repo (base64 decodificado)
  create_branch(owner, repo, branch, sha)    — crea una rama desde un commit/sha
  create_commit(owner, repo, path, ...)      — crea o actualiza un archivo con un commit
  create_pull_request(owner, repo, ...)      — abre un Pull Request

Autenticación: variable de entorno GITHUB_TOKEN (Personal Access Token o GitHub App token).
Si no está configurado, las llamadas funcionan con rate limit reducido (60 req/h).
"""
from __future__ import annotations

import base64
import logging
import os

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.github")

_GITHUB_API    = "https://api.github.com"
_GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")

# Headers por defecto para todas las requests
def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return h


# ── Inputs ────────────────────────────────────────────────────────────────────

class GetRepoInput(ToolInput):
    owner: str
    repo:  str

class ListIssuesInput(ToolInput):
    owner:  str
    repo:   str
    state:  str = "open"   # open | closed | all
    labels: str = ""       # Comma-separated label names
    limit:  int = 20

class CreateIssueInput(ToolInput):
    owner:  str
    repo:   str
    title:  str
    body:   str = ""
    labels: list[str] = []

class ListPullRequestsInput(ToolInput):
    owner: str
    repo:  str
    state: str = "open"    # open | closed | all
    limit: int = 20

class GetWorkflowRunsInput(ToolInput):
    owner:    str
    repo:     str
    workflow: str = ""     # workflow filename o ID; "" = todos
    limit:    int = 10

class GetFileContentsInput(ToolInput):
    owner: str
    repo:  str
    path:  str             # path dentro del repo (ej. "src/main.py")
    ref:   str = "main"    # branch, tag o commit SHA

class CreateBranchInput(ToolInput):
    owner:      str
    repo:       str
    branch:     str        # nombre de la nueva rama (ej. "feat/gabriel-fix-123")
    from_ref:   str = "main"  # rama/sha origen

class CreateCommitInput(ToolInput):
    owner:      str
    repo:       str
    path:       str        # path del archivo a crear/actualizar
    content:    str        # contenido completo del archivo (texto plano)
    message:    str        # mensaje del commit
    branch:     str        # rama donde commitear
    sha:        str = ""   # SHA actual del archivo (requerido para updates, "" para nuevos)

class CreatePullRequestInput(ToolInput):
    owner:      str
    repo:       str
    title:      str
    body:       str = ""
    head:       str        # rama de origen (feature branch)
    base:       str = "main"  # rama destino
    draft:      bool = False


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class GitHubTool(BaseTool):
    """
    Integración con GitHub API: repositorios, issues, PRs y workflows CI/CD.
    Usada por CoderAgent y SREAgent para trazabilidad de cambios y CI/CD.
    """

    name            = "github"
    description     = "GitHub API: repos, issues, pull requests, workflow runs y escritura de código (Gabriel)"
    version         = "2.0.0"
    external_system = "github"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, GetRepoInput):
            return await self.get_repo(input)
        if isinstance(input, ListIssuesInput):
            return await self.list_issues(input)
        if isinstance(input, CreateIssueInput):
            return await self.create_issue(input)
        if isinstance(input, ListPullRequestsInput):
            return await self.list_pull_requests(input)
        if isinstance(input, GetWorkflowRunsInput):
            return await self.get_workflow_runs(input)
        if isinstance(input, GetFileContentsInput):
            return await self.get_file_contents(input)
        if isinstance(input, CreateBranchInput):
            return await self.create_branch(input)
        if isinstance(input, CreateCommitInput):
            return await self.create_commit(input)
        if isinstance(input, CreatePullRequestInput):
            return await self.create_pull_request(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def get_repo(self, input: GetRepoInput) -> ToolOutput:
        """Obtiene metadata del repositorio (stars, forks, description, language, topics)."""
        try:
            resp = _req.get(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}",
                headers=_headers(),
                timeout=10,
            )
            if resp.status_code == 404:
                return ToolOutput.fail(
                    f"Repositorio '{input.owner}/{input.repo}' no encontrado",
                    source=self.name,
                )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"GitHub API HTTP {resp.status_code}",
                    source=self.name,
                )
            d = resp.json()
            return ToolOutput.ok(
                data={
                    "full_name":    d.get("full_name"),
                    "description":  d.get("description"),
                    "language":     d.get("language"),
                    "stars":        d.get("stargazers_count", 0),
                    "forks":        d.get("forks_count", 0),
                    "open_issues":  d.get("open_issues_count", 0),
                    "default_branch": d.get("default_branch", "main"),
                    "url":          d.get("html_url"),
                    "topics":       d.get("topics", []),
                    "archived":     d.get("archived", False),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] get_repo error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def list_issues(self, input: ListIssuesInput) -> ToolOutput:
        """Lista issues del repositorio con filtros de estado y labels."""
        try:
            params: dict = {"state": input.state, "per_page": min(input.limit, 100)}
            if input.labels:
                params["labels"] = input.labels

            resp = _req.get(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/issues",
                headers=_headers(),
                params=params,
                timeout=10,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"GitHub API HTTP {resp.status_code}",
                    source=self.name,
                )
            issues = resp.json()[: input.limit]
            simplified = [
                {
                    "number":  i.get("number"),
                    "title":   i.get("title"),
                    "state":   i.get("state"),
                    "labels":  [lb.get("name") for lb in i.get("labels", [])],
                    "author":  i.get("user", {}).get("login"),
                    "created": i.get("created_at"),
                    "url":     i.get("html_url"),
                }
                for i in issues
                if not i.get("pull_request")   # excluir PRs de la lista de issues
            ]
            return ToolOutput.ok(
                data=simplified,
                source=self.name,
                count=len(simplified),
                repo=f"{input.owner}/{input.repo}",
            )
        except Exception as exc:
            logger.error(f"[github_tool] list_issues error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def create_issue(self, input: CreateIssueInput) -> ToolOutput:
        """Crea un nuevo issue en el repositorio."""
        try:
            payload: dict = {"title": input.title, "body": input.body}
            if input.labels:
                payload["labels"] = input.labels

            resp = _req.post(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/issues",
                headers=_headers(),
                json=payload,
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"GitHub API HTTP {resp.status_code}: {resp.text[:300]}",
                    source=self.name,
                )
            issue = resp.json()
            return ToolOutput.ok(
                data={
                    "number": issue.get("number"),
                    "url":    issue.get("html_url"),
                    "title":  issue.get("title"),
                    "state":  issue.get("state"),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] create_issue error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def list_pull_requests(self, input: ListPullRequestsInput) -> ToolOutput:
        """Lista pull requests del repositorio."""
        try:
            resp = _req.get(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/pulls",
                headers=_headers(),
                params={"state": input.state, "per_page": min(input.limit, 100)},
                timeout=10,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"GitHub API HTTP {resp.status_code}",
                    source=self.name,
                )
            prs = resp.json()[: input.limit]
            simplified = [
                {
                    "number":  pr.get("number"),
                    "title":   pr.get("title"),
                    "state":   pr.get("state"),
                    "author":  pr.get("user", {}).get("login"),
                    "base":    pr.get("base", {}).get("ref"),
                    "head":    pr.get("head", {}).get("ref"),
                    "draft":   pr.get("draft", False),
                    "created": pr.get("created_at"),
                    "url":     pr.get("html_url"),
                }
                for pr in prs
            ]
            return ToolOutput.ok(
                data=simplified,
                source=self.name,
                count=len(simplified),
                repo=f"{input.owner}/{input.repo}",
            )
        except Exception as exc:
            logger.error(f"[github_tool] list_pull_requests error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def get_workflow_runs(self, input: GetWorkflowRunsInput) -> ToolOutput:
        """Obtiene las últimas ejecuciones de un workflow CI/CD."""
        try:
            if input.workflow:
                url = (
                    f"{_GITHUB_API}/repos/{input.owner}/{input.repo}"
                    f"/actions/workflows/{input.workflow}/runs"
                )
            else:
                url = (
                    f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/actions/runs"
                )

            resp = _req.get(
                url,
                headers=_headers(),
                params={"per_page": min(input.limit, 100)},
                timeout=10,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"GitHub API HTTP {resp.status_code}",
                    source=self.name,
                )
            runs = resp.json().get("workflow_runs", [])[: input.limit]
            simplified = [
                {
                    "id":         r.get("id"),
                    "name":       r.get("name"),
                    "status":     r.get("status"),        # queued | in_progress | completed
                    "conclusion": r.get("conclusion"),    # success | failure | cancelled | ...
                    "branch":     r.get("head_branch"),
                    "commit_sha": r.get("head_sha", "")[:8],
                    "created":    r.get("created_at"),
                    "url":        r.get("html_url"),
                }
                for r in runs
            ]
            return ToolOutput.ok(
                data=simplified,
                source=self.name,
                count=len(simplified),
                repo=f"{input.owner}/{input.repo}",
            )
        except Exception as exc:
            logger.error(f"[github_tool] get_workflow_runs error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def get_file_contents(self, input: GetFileContentsInput) -> ToolOutput:
        """Lee el contenido de un archivo del repositorio (decodifica base64)."""
        try:
            resp = _req.get(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/contents/{input.path}",
                headers=_headers(),
                params={"ref": input.ref},
                timeout=10,
            )
            if resp.status_code == 404:
                return ToolOutput.fail(
                    f"Archivo '{input.path}' no encontrado en {input.owner}/{input.repo}@{input.ref}",
                    source=self.name,
                )
            if resp.status_code != 200:
                return ToolOutput.fail(f"GitHub API HTTP {resp.status_code}", source=self.name)
            d = resp.json()
            content_b64 = d.get("content", "").replace("\n", "")
            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            return ToolOutput.ok(
                data={
                    "path":    d.get("path"),
                    "sha":     d.get("sha"),
                    "size":    d.get("size"),
                    "content": content,
                    "url":     d.get("html_url"),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] get_file_contents error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def create_branch(self, input: CreateBranchInput) -> ToolOutput:
        """Crea una nueva rama a partir de from_ref (resuelve el SHA automáticamente)."""
        try:
            # Resolver SHA del from_ref
            ref_resp = _req.get(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/git/ref/heads/{input.from_ref}",
                headers=_headers(),
                timeout=10,
            )
            if ref_resp.status_code != 200:
                return ToolOutput.fail(
                    f"No se pudo resolver la rama origen '{input.from_ref}': HTTP {ref_resp.status_code}",
                    source=self.name,
                )
            sha = ref_resp.json()["object"]["sha"]

            resp = _req.post(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/git/refs",
                headers=_headers(),
                json={"ref": f"refs/heads/{input.branch}", "sha": sha},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return ToolOutput.ok(
                    data={"branch": input.branch, "sha": sha, "from_ref": input.from_ref},
                    source=self.name,
                )
            if resp.status_code == 422:
                return ToolOutput.fail(
                    f"La rama '{input.branch}' ya existe.",
                    source=self.name,
                )
            return ToolOutput.fail(
                f"GitHub API HTTP {resp.status_code}: {resp.text[:300]}",
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] create_branch error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def create_commit(self, input: CreateCommitInput) -> ToolOutput:
        """Crea o actualiza un archivo con un commit en la rama indicada."""
        try:
            content_b64 = base64.b64encode(input.content.encode("utf-8")).decode()
            payload: dict = {
                "message": input.message,
                "content": content_b64,
                "branch":  input.branch,
            }
            if input.sha:
                payload["sha"] = input.sha  # requerido para updates

            resp = _req.put(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/contents/{input.path}",
                headers=_headers(),
                json=payload,
                timeout=20,
            )
            if resp.status_code in (200, 201):
                d = resp.json()
                return ToolOutput.ok(
                    data={
                        "path":       d["content"]["path"],
                        "sha":        d["content"]["sha"],
                        "commit_sha": d["commit"]["sha"],
                        "commit_url": d["commit"]["html_url"],
                    },
                    source=self.name,
                )
            return ToolOutput.fail(
                f"GitHub API HTTP {resp.status_code}: {resp.text[:300]}",
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] create_commit error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def create_pull_request(self, input: CreatePullRequestInput) -> ToolOutput:
        """Abre un Pull Request en el repositorio."""
        try:
            resp = _req.post(
                f"{_GITHUB_API}/repos/{input.owner}/{input.repo}/pulls",
                headers=_headers(),
                json={
                    "title": input.title,
                    "body":  input.body,
                    "head":  input.head,
                    "base":  input.base,
                    "draft": input.draft,
                },
                timeout=20,
            )
            if resp.status_code in (200, 201):
                pr = resp.json()
                return ToolOutput.ok(
                    data={
                        "number":  pr.get("number"),
                        "url":     pr.get("html_url"),
                        "title":   pr.get("title"),
                        "state":   pr.get("state"),
                        "draft":   pr.get("draft", False),
                        "head":    pr.get("head", {}).get("ref"),
                        "base":    pr.get("base", {}).get("ref"),
                    },
                    source=self.name,
                )
            if resp.status_code == 422:
                detail = resp.json().get("errors", [{}])[0].get("message", resp.text[:200])
                return ToolOutput.fail(f"PR ya existe o parámetros inválidos: {detail}", source=self.name)
            return ToolOutput.fail(
                f"GitHub API HTTP {resp.status_code}: {resp.text[:300]}",
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[github_tool] create_pull_request error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def health_check(self) -> bool:
        """Verifica que la GitHub API responde (usa /zen endpoint sin auth)."""
        try:
            resp = _req.get(
                f"{_GITHUB_API}/zen",
                headers=_headers(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning(f"[github_tool] health_check falló: {exc}")
            return False
