"""
Camael — Agente de DevOps: CI/CD, Kubernetes y operaciones de entrega.

Angelología: Camael (כַּמָאֵל) es el ángel de la fuerza, el coraje y la ejecución.

A diferencia de Raphael (SRE reactivo, loop autónomo), Camael es activado
por el usuario para gestionar despliegues, pipelines y operaciones de entrega.

Diferencia clave:
  Raphael → observa, detecta y sana (autonomía, 60s loop)
  Camael  → ejecuta lo que el usuario ordena (deploy, scale, trigger CI/CD)

Capacidades:
  k8s_status        — estado de deployments en un namespace
  k8s_scale         — escala un deployment a N réplicas
  k8s_rollout       — ejecuta rollout restart de un deployment
  workflow_list     — lista workflows de GitHub Actions de un repo
  workflow_trigger  — dispara un workflow en GitHub Actions
  workflow_status   — consulta el estado de un run de workflow
  (default)         — modo conversacional con RAG

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("camael", ctx)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.camael")

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_CONVERSATIONAL = """\
Eres Camael, un experto en DevOps, CI/CD y operaciones de infraestructura.
Especializado en Kubernetes (MicroK8s), GitHub Actions, Docker y automatización de despliegues.

Stack del proyecto:
- Orquestación: MicroK8s single-node, namespace amael-ia
- CI/CD: GitHub Actions (.github/workflows/ci.yml), runner self-hosted amael-lab
- Registry: registry.richardx.dev (TLS-only, sin auth HTTP)
- Monitoreo: Prometheus + Grafana, OTel → Tempo
- Branching: develop (default) → PR → main → auto-deploy

Directrices:
- Incluye comandos kubectl/docker/gh exactos cuando sea útil
- Explica el impacto de cada operación antes de ejecutarla
- Alerta sobre operaciones destructivas o de alto riesgo
- Responde en el mismo idioma de la pregunta"""

# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class CamaelAgent(BaseAgent):
    """
    Camael — Agente de DevOps y operaciones de entrega.

    task dict para modo conversacional (default):
        {"query": str, "user_id": str}

    task dict para operaciones K8s:
        {"task": "k8s_status",  "namespace": str,        "user_id": str}
        {"task": "k8s_scale",   "deployment": str, "replicas": int, "namespace": str, "user_id": str}
        {"task": "k8s_rollout", "deployment": str, "namespace": str, "user_id": str}

    task dict para GitHub Actions:
        {"task": "workflow_list",    "owner": str, "repo": str,                           "user_id": str}
        {"task": "workflow_trigger", "owner": str, "repo": str, "workflow": str, "ref": str, "user_id": str}
        {"task": "workflow_status",  "owner": str, "repo": str, "run_id": str,             "user_id": str}
    """

    name         = "camael"
    role         = "DevOps: CI/CD, Kubernetes y operaciones de entrega"
    version      = "1.0.0"
    capabilities = [
        "k8s_operations",
        "cicd_management",
        "deployment_automation",
        "github_actions",
        "rag_retrieval",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        task_type = task.get("task", "").lower()
        if task_type == "k8s_status":       return await self._k8s_status(task)
        if task_type == "k8s_scale":        return await self._k8s_scale(task)
        if task_type == "k8s_rollout":      return await self._k8s_rollout(task)
        if task_type == "workflow_list":    return await self._workflow_list(task)
        if task_type == "workflow_trigger": return await self._workflow_trigger(task)
        if task_type == "workflow_status":  return await self._workflow_status(task)
        return await self._conversational(task)

    # ── Conversacional ────────────────────────────────────────────────────────

    async def _conversational(self, task: Dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_id", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="query vacía")

        rag_ctx = await retrieve_rag_context(user_email, query, k=3, agent_name=self.name)
        prompt  = build_prompt(_SYSTEM_CONVERSATIONAL, query, rag_ctx,
                               context_header="## Contexto de infraestructura",
                               question_header="## Pregunta DevOps")
        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "camael"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_ctx)},
            )
        except Exception as exc:
            logger.error(f"[camael] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── K8s operations ────────────────────────────────────────────────────────

    async def _k8s_status(self, task: Dict[str, Any]) -> AgentResult:
        namespace  = task.get("namespace", "amael-ia")
        user_email = task.get("user_id", "")
        query      = f"Dame el estado de todos los deployments en el namespace {namespace}: pods running/pending/failed, imágenes actuales y últimos eventos."
        return await self._k8s_query(query, user_email, task_name="k8s_status",
                                     metadata={"namespace": namespace})

    async def _k8s_scale(self, task: Dict[str, Any]) -> AgentResult:
        deployment = task.get("deployment", "")
        replicas   = int(task.get("replicas", 1))
        namespace  = task.get("namespace", "amael-ia")
        user_email = task.get("user_id", "")

        if not deployment:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'deployment' es requerido para k8s_scale")

        query = f"Escala el deployment '{deployment}' en namespace '{namespace}' a {replicas} réplicas. Confirma el resultado."
        return await self._k8s_query(query, user_email, task_name="k8s_scale",
                                     metadata={"deployment": deployment, "replicas": replicas,
                                               "namespace": namespace})

    async def _k8s_rollout(self, task: Dict[str, Any]) -> AgentResult:
        deployment = task.get("deployment", "")
        namespace  = task.get("namespace", "amael-ia")
        user_email = task.get("user_id", "")

        if not deployment:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'deployment' es requerido para k8s_rollout")

        query = f"Ejecuta rollout restart del deployment '{deployment}' en namespace '{namespace}' y reporta el estado del rollout."
        return await self._k8s_query(query, user_email, task_name="k8s_rollout",
                                     metadata={"deployment": deployment, "namespace": namespace})

    async def _k8s_query(
        self,
        query: str,
        user_email: str,
        task_name: str,
        metadata: Dict,
    ) -> AgentResult:
        """Delega una operación K8s al k8s-agent service."""
        import asyncio
        import httpx
        try:
            from config.settings import settings
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{settings.k8s_agent_url}/api/k8s-agent",
                    json={"query": query, "user_email": user_email},
                    headers={"Authorization": f"Bearer {settings.internal_api_secret}"},
                )
            if resp.status_code == 200:
                data     = resp.json()
                response = data.get("response") or data.get("answer") or str(data)
                return AgentResult(
                    success=True,
                    output={"response": response, **metadata, "task": task_name},
                    agent_name=self.name,
                )
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"k8s-agent {resp.status_code}: {resp.text[:300]}",
            )
        except Exception as exc:
            logger.error(f"[camael] k8s_query error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── GitHub Actions ────────────────────────────────────────────────────────

    async def _workflow_list(self, task: Dict[str, Any]) -> AgentResult:
        owner = task.get("owner", "")
        repo  = task.get("repo", "")
        if not owner or not repo:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'owner' y 'repo' son requeridos")
        try:
            token = _get_github_token()
            data  = await _gh_get(f"repos/{owner}/{repo}/actions/workflows", token)
            workflows = [
                {"id": w["id"], "name": w["name"], "path": w["path"], "state": w["state"]}
                for w in data.get("workflows", [])
            ]
            return AgentResult(
                success=True,
                output={"workflows": workflows, "total": data.get("total_count", 0),
                        "task": "workflow_list"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] workflow_list error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    async def _workflow_trigger(self, task: Dict[str, Any]) -> AgentResult:
        owner    = task.get("owner", "")
        repo     = task.get("repo", "")
        workflow = task.get("workflow", "")   # filename or id, e.g. "ci.yml"
        ref      = task.get("ref", "main")
        inputs   = task.get("inputs", {})

        if not all([owner, repo, workflow]):
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'owner', 'repo' y 'workflow' son requeridos")
        try:
            token = _get_github_token()
            await _gh_post(
                f"repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
                token,
                {"ref": ref, "inputs": inputs},
            )
            logger.info(f"[camael] Workflow '{workflow}' disparado en {owner}/{repo}@{ref}")
            return AgentResult(
                success=True,
                output={"triggered": True, "workflow": workflow, "ref": ref,
                        "task": "workflow_trigger"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] workflow_trigger error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    async def _workflow_status(self, task: Dict[str, Any]) -> AgentResult:
        owner  = task.get("owner", "")
        repo   = task.get("repo", "")
        run_id = task.get("run_id", "")

        if run_id:
            # Estado de un run específico
            if not all([owner, repo]):
                return AgentResult(success=False, output=None, agent_name=self.name,
                                   error="'owner' y 'repo' son requeridos")
            try:
                token = _get_github_token()
                data  = await _gh_get(f"repos/{owner}/{repo}/actions/runs/{run_id}", token)
                return AgentResult(
                    success=True,
                    output={
                        "run_id":     data.get("id"),
                        "name":       data.get("name"),
                        "status":     data.get("status"),      # queued/in_progress/completed
                        "conclusion": data.get("conclusion"),  # success/failure/cancelled/None
                        "branch":     data.get("head_branch"),
                        "commit":     data.get("head_sha", "")[:8],
                        "url":        data.get("html_url"),
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "task":       "workflow_status",
                    },
                    agent_name=self.name,
                )
            except Exception as exc:
                logger.error(f"[camael] workflow_status error: {exc}")
                return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))
        else:
            # Últimos runs del repo
            if not all([owner, repo]):
                return AgentResult(success=False, output=None, agent_name=self.name,
                                   error="'owner' y 'repo' son requeridos (o 'run_id' para un run específico)")
            try:
                token = _get_github_token()
                data  = await _gh_get(
                    f"repos/{owner}/{repo}/actions/runs?per_page=5",
                    token,
                )
                runs = [
                    {
                        "run_id":     r["id"],
                        "name":       r["name"],
                        "status":     r["status"],
                        "conclusion": r["conclusion"],
                        "branch":     r["head_branch"],
                        "commit":     r["head_sha"][:8],
                        "url":        r["html_url"],
                        "created_at": r["created_at"],
                    }
                    for r in data.get("workflow_runs", [])
                ]
                return AgentResult(
                    success=True,
                    output={"runs": runs, "task": "workflow_status"},
                    agent_name=self.name,
                )
            except Exception as exc:
                logger.error(f"[camael] workflow_status (list) error: {exc}")
                return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _get_github_token() -> str:
    import os
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN no configurado")
    return token


async def _gh_get(path: str, token: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.github.com/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _gh_post(path: str, token: str, body: dict) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://api.github.com/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=body,
        )
        # workflow_dispatch returns 204 No Content on success
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"GitHub API {resp.status_code}: {resp.text[:300]}")
