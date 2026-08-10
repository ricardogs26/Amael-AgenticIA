"""
QAAgent (Phanuel) — Agente de calidad: ejecuta tests y reporta resultados.

Modos de operación:
  run       — dispara workflow_dispatch en GitHub Actions y hace polling del resultado
  status    — consulta el estado de la última ejecución del workflow de tests
  conversational (default) — responde preguntas sobre testing y calidad

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("qa", ctx)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.qa")

_GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
_DEFAULT_OWNER = os.environ.get("GABRIEL_GITHUB_OWNER", "ricardogs26")
_DEFAULT_REPO  = os.environ.get("GABRIEL_GITHUB_REPO", "Amael-AgenticIA")
_WORKFLOW_FILE = "ci.yml"
_POLL_INTERVAL = 10   # segundos entre polls
_POLL_TIMEOUT  = 300  # máximo 5 min esperando resultado

# Componentes con CI disparable. Cada uno resuelve a (owner, repo, rama default).
# Los sinónimos son lo que el usuario dice en lenguaje natural.
_COMPONENTS: dict[str, tuple[str, str, str]] = {
    "backend":  (_DEFAULT_OWNER, "Amael-AgenticIA", "main"),
    "amael":    (_DEFAULT_OWNER, "Amael-AgenticIA", "main"),
    "agentic":  (_DEFAULT_OWNER, "Amael-AgenticIA", "main"),
    "raphael":  (_DEFAULT_OWNER, "Amael-AgenticIA", "main"),  # comparte suite
    "camael":   (_DEFAULT_OWNER, "Amael-AgenticIA", "main"),  # comparte suite
    "trader":   (_DEFAULT_OWNER, "trader-service",  "main"),
}


def _load_kb(filename: str) -> str:
    """Carga la base de conocimiento de QA (catálogo de tests). Patrón _load_kb
    de Raphael: llaves escapadas para no romper PromptTemplate de LangChain."""
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        content = open(path).read().replace("{", "{{").replace("}", "}}")
        logger.info(f"[qa] Catálogo de tests cargado desde {path} ({len(content)} chars)")
        return content
    except Exception as exc:
        logger.warning(f"[qa] No se pudo cargar {filename}: {exc}")
        return ""


_QA_KNOWLEDGE = _load_kb("qa_knowledge.md")

_SYSTEM_CONVERSATIONAL = """Eres Phanuel, el agente de QA de Amael-IA. Conoces el catálogo COMPLETO de pruebas del cluster (abajo). Tu trabajo:
- Recomendar QUÉ tests correr tras un cambio concreto (usa la sección «cuándo correr qué»).
- Dar el comando EXACTO de ejecución del componente/suite que aplique.
- Explicar qué cubre cada suite y los gotchas conocidos.
- Para disparar tests de verdad, el usuario dice «corre los tests de <componente>».

Responde en el idioma de la pregunta, conciso y con comandos copiables. Si el usuario menciona un archivo o módulo que tocó, mapéalo al subconjunto de tests que lo cubre — no mandes a correr todo salvo que sea antes de un merge.

═══════════ CATÁLOGO DE PRUEBAS (tu memoria) ═══════════
{kb}
═══════════════════════════════════════════════════════""".replace("{kb}", _QA_KNOWLEDGE)

_RUN_TRIGGERS = (
    "ejecuta", "ejecutar", "corre", "correr", "lanza", "lanzar",
    "run tests", "run the tests", "run pytest", "dispara", "dispara los tests",
    "prueba", "pruebas", "tests ahora", "testea",
)
_STATUS_TRIGGERS = (
    "estado", "status", "resultado", "results", "última ejecución",
    "último run", "pasaron", "fallaron", "check tests",
)


# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class QAAgent(BaseAgent):
    """
    Phanuel — Agente de calidad que ejecuta la suite de tests vía GitHub Actions.

    task dict esperado:
        {"query": str, "user_email": str}

    El modo se detecta automáticamente de la query:
      - keywords de ejecución  → dispara workflow + polling
      - keywords de status     → consulta última ejecución
      - default                → conversacional
    """

    name         = "qa"
    role         = "QA: catálogo de pruebas del cluster + ejecución multi-repo de tests"
    version      = "1.1.0"
    capabilities = [
        "run_tests",          # dispara el CI del componente pedido (backend | trader)
        "poll_ci_results",
        "test_status",
        "test_catalog",       # conoce todos los tests del cluster (qa_knowledge.md)
        "test_recommendation", # recomienda qué correr tras un cambio concreto
        "coverage_analysis",
    ]

    def _detect_mode(self, query: str) -> str:
        q = query.lower().strip()
        # Una PREGUNTA nunca dispara el CI, aunque diga «correr»: «¿qué tests
        # debo correr?» pide recomendación desde el catálogo, no una ejecución
        # real de 5 min de polling (10-ago: por WhatsApp eso es inaceptable).
        # Solo el imperativo claro ejecuta.
        es_pregunta = q.startswith(("¿", "que ", "qué ", "cual", "cuál", "como",
                                    "cómo", "recomien", "deberia", "debería",
                                    "debo ", "cuales", "cuáles")) or q.endswith("?")
        if any(kw in q for kw in _RUN_TRIGGERS) and not es_pregunta:
            return "run"
        if any(kw in q for kw in _STATUS_TRIGGERS):
            return "status"
        return "conversational"

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        query = task.get("query", "").strip()
        mode  = task.get("mode") or self._detect_mode(query)

        if mode == "run":
            return await self._run_tests(task)
        if mode == "status":
            return await self._get_status(task)
        return await self._conversational(task)

    # ── Modo: ejecutar tests ───────────────────────────────────────────────────

    def _resolve_component(self, query: str) -> tuple[str, str, str]:
        """Del texto del usuario saca (owner, repo, rama). Default: backend."""
        q = query.lower()
        for alias, target in _COMPONENTS.items():
            if alias in q:
                return target
        return _COMPONENTS["backend"]

    async def _run_tests(self, task: dict[str, Any]) -> AgentResult:
        query  = task.get("query", "")
        # El componente se infiere del texto («corre los tests del trader»); un
        # override explícito en el task gana. Antes solo existía un repo.
        c_owner, c_repo, c_ref = self._resolve_component(query)
        owner  = task.get("github_owner", c_owner)
        repo   = task.get("github_repo",  c_repo)
        ref    = task.get("ref", c_ref)

        if not _GITHUB_TOKEN:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="GITHUB_TOKEN no configurado",
            )

        await self._notify(f"🧪 *Phanuel iniciando tests*\nRepo: `{owner}/{repo}` rama: `{ref}`")
        logger.info(f"[qa] Disparando workflow en {owner}/{repo}@{ref}")

        # 1. Disparar workflow_dispatch
        reason = query[:80] or "chat on-demand"
        dispatch_ok = await self._dispatch_workflow(owner, repo, ref, reason=reason)
        if not dispatch_ok:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=(
                    "No se pudo disparar el workflow. Verifica que ci.yml tenga "
                    "workflow_dispatch y que el GITHUB_TOKEN tenga scope 'workflow'."
                ),
            )

        # 2. Esperar a que aparezca el run (GitHub tarda ~2s en registrarlo)
        await asyncio.sleep(5)

        # 3. Polling hasta resultado
        result = await self._poll_latest_run(owner, repo, ref)
        return result

    async def _dispatch_workflow(self, owner: str, repo: str, ref: str, reason: str) -> bool:
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{_WORKFLOW_FILE}/dispatches"
        payload = {"ref": ref, "inputs": {"ref": ref, "reason": reason}}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=_gh_headers())
        if resp.status_code == 204:
            return True
        logger.error(f"[qa] dispatch falló {resp.status_code}: {resp.text[:200]}")
        return False

    async def _poll_latest_run(self, owner: str, repo: str, ref: str) -> AgentResult:
        url      = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{_WORKFLOW_FILE}/runs"
        deadline = time.monotonic() + _POLL_TIMEOUT
        run_id   = None

        # Buscar el run más reciente en la rama solicitada
        while time.monotonic() < deadline:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url, params={"branch": ref, "per_page": 5}, headers=_gh_headers()
                )
            if resp.status_code != 200:
                break
            runs = resp.json().get("workflow_runs", [])
            if runs:
                latest = runs[0]
                run_id = latest["id"]
                status = latest["status"]    # queued | in_progress | completed

                if status == "completed":
                    return self._build_result(owner, repo, latest)

                logger.debug(f"[qa] run={run_id} status={status} — esperando...")
            await asyncio.sleep(_POLL_INTERVAL)

        # Timeout
        run_url = (
            f"https://github.com/{owner}/{repo}/actions/runs/{run_id}"
            if run_id else f"https://github.com/{owner}/{repo}/actions"
        )
        await self._notify(
            f"⏱️ *Phanuel*: Timeout esperando resultado. "
            f"Verifica manualmente: {run_url}"
        )
        return AgentResult(
            success=False, output={"run_url": run_url}, agent_name=self.name,
            error=f"Timeout ({_POLL_TIMEOUT}s) esperando resultado del workflow",
        )

    def _build_result(self, owner: str, repo: str, run: dict) -> AgentResult:
        conclusion  = run.get("conclusion", "unknown")
        run_id      = run["id"]
        run_url     = run["html_url"]
        duration_s  = _calc_duration(run)
        commit_sha  = run.get("head_sha", "")[:8]
        commit_msg  = run.get("head_commit", {}).get("message", "")[:60]

        icon = "✅" if conclusion == "success" else "❌" if conclusion == "failure" else "⚠️"
        summary = (
            f"{icon} *Tests {conclusion.upper()}*\n"
            f"• Repo: `{owner}/{repo}`\n"
            f"• Commit: `{commit_sha}` — {commit_msg}\n"
            f"• Duración: {duration_s}s\n"
            f"• Detalles: {run_url}"
        )

        asyncio.ensure_future(self._notify(summary))
        logger.info(f"[qa] run={run_id} conclusion={conclusion} ({duration_s}s)")

        return AgentResult(
            success=(conclusion == "success"),
            output={
                "conclusion":  conclusion,
                "run_id":      run_id,
                "run_url":     run_url,
                "duration_s":  duration_s,
                "commit_sha":  commit_sha,
                "commit_msg":  commit_msg,
                "response":    summary.replace("*", "").replace("`", ""),
            },
            agent_name=self.name,
            metadata={"owner": owner, "repo": repo, "mode": "run"},
        )

    # ── Modo: status de última ejecución ──────────────────────────────────────

    async def _get_status(self, task: dict[str, Any]) -> AgentResult:
        c_owner, c_repo, c_ref = self._resolve_component(task.get("query", ""))
        owner = task.get("github_owner", c_owner)
        repo  = task.get("github_repo",  c_repo)
        ref   = task.get("ref", c_ref)

        if not _GITHUB_TOKEN:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="GITHUB_TOKEN no configurado",
            )

        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{_WORKFLOW_FILE}/runs"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url, params={"branch": ref, "per_page": 3}, headers=_gh_headers()
            )

        if resp.status_code != 200:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"GitHub API error {resp.status_code}",
            )

        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return AgentResult(
                success=True,
                output={"response": f"No hay ejecuciones previas del workflow en la rama `{ref}`."},
                agent_name=self.name,
            )

        lines = [f"Últimas ejecuciones de tests en `{owner}/{repo}` (rama `{ref}`):\n"]
        for r in runs[:3]:
            icon       = "✅" if r.get("conclusion") == "success" else \
                         "❌" if r.get("conclusion") == "failure" else "🔄"
            conclusion = r.get("conclusion") or r.get("status", "?")
            sha        = r.get("head_sha", "")[:8]
            msg        = r.get("head_commit", {}).get("message", "")[:50]
            dur        = _calc_duration(r)
            lines.append(f"{icon} `{sha}` {msg} — {conclusion} ({dur}s)\n   {r['html_url']}")

        response = "\n".join(lines)
        return AgentResult(
            success=True,
            output={"response": response, "runs": [r["id"] for r in runs[:3]]},
            agent_name=self.name,
        )

    # ── Modo: conversacional ───────────────────────────────────────────────────

    async def _conversational(self, task: dict[str, Any]) -> AgentResult:
        query = task.get("query", "").strip()
        if not query:
            return AgentResult(
                success=False, output=None, agent_name=self.name, error="query vacía"
            )

        prompt = build_prompt(_SYSTEM_CONVERSATIONAL, query)
        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "qa"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[qa] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _notify(self, message: str) -> None:
        try:
            bridge_url = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
            phone      = os.environ.get("ADMIN_PHONE", "")
            if not phone or not bridge_url:
                return
            import requests
            await asyncio.to_thread(
                requests.post,
                f"{bridge_url}/send",
                json={"phoneNumber": phone, "text": message},
                timeout=5,
            )
        except Exception as exc:
            logger.debug(f"[qa] notify falló (no crítico): {exc}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _calc_duration(run: dict) -> int:
    try:
        from datetime import UTC, datetime
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(run["created_at"], fmt).replace(tzinfo=UTC)
        end   = datetime.strptime(run["updated_at"], fmt).replace(tzinfo=UTC)
        return int((end - start).total_seconds())
    except Exception:
        return 0
