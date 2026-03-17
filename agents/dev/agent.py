"""
Gabriel — Agente de desarrollo autónomo: código, bugs, PRs y refactoring en GitHub.

Modos de operación:
  conversational (default) — responde preguntas de desarrollo con RAG + LLM
  autonomous               — ciclo completo: analiza → lee → genera → commitea → abre PR

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("gabriel", ctx)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional

import requests

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.gabriel")

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_CONVERSATIONAL = """Eres Gabriel, un ingeniero de software senior especializado en Python, FastAPI, Next.js y
arquitecturas de sistemas distribuidos. Trabajas en Amael-IA, una plataforma multi-agente con LangGraph.

Directrices:
- Escribe código limpio, explícito y sin over-engineering
- Prefiere editar archivos existentes antes de crear nuevos
- Incluye solo los comentarios donde la lógica no sea obvia
- Cuando detectes un bug, explica la causa raíz antes de proponer el fix
- Para refactoring, justifica el cambio con el problema concreto que resuelve
- Responde siempre en el mismo idioma que la pregunta

Stack técnico del proyecto:
- Backend: Python 3.11, FastAPI, LangGraph, LangChain, Pydantic v2
- Frontend: Next.js 14 (App Router), TypeScript, React 18
- Infra: Kubernetes (MicroK8s), Docker, PostgreSQL, Redis, Qdrant
- LLM: Ollama (qwen2.5:14b), nomic-embed-text para embeddings"""

_SYSTEM_ANALYZE = """Eres Gabriel, un ingeniero senior que analiza solicitudes de cambio de código.
Dado un repo de GitHub y una descripción de tarea, determina:
- qué archivo modificar
- nombre de la rama (formato: feat/gabriel-<slug>)
- mensaje de commit
- título y descripción del PR

Responde EXCLUSIVAMENTE con JSON válido (sin markdown, sin texto extra):
{
  "target_file": "<ruta relativa del archivo a modificar>",
  "branch_name": "<feat/gabriel-slug-descriptivo>",
  "commit_message": "<tipo: descripción concisa>",
  "pr_title": "<título claro del PR>",
  "pr_body": "<descripción del cambio, causa raíz si es bug, enfoque del fix>"
}"""

_SYSTEM_GENERATE = """Eres Gabriel, un ingeniero senior modificando un archivo de código.
Se te proporciona el contenido ACTUAL del archivo y la tarea a realizar.
Tu respuesta debe contener ÚNICAMENTE el contenido completo del archivo modificado.
No incluyas explicaciones, markdown, ni bloques de código ``` — solo el código puro.
Marcadores de inicio y fin:
---FILE_START---
<contenido del archivo>
---FILE_END---"""


# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class GabrielAgent(BaseAgent):
    """
    Gabriel — Agente de desarrollo autónomo.

    task dict esperado (modo conversacional):
        {"query": str, "user_email": str}

    task dict esperado (modo autónomo):
        {
            "mode":          "autonomous",
            "query":         str,          # descripción de la tarea
            "github_owner":  str,          # opcional; usa GABRIEL_GITHUB_OWNER si no se indica
            "github_repo":   str,          # opcional; usa GABRIEL_GITHUB_REPO si no se indica
            "target_file":   str,          # opcional; LLM lo determina si no se indica
            "base_branch":   str,          # opcional; default "main"
            "user_email":    str,          # para notificación y RAG
        }
    """

    name         = "gabriel"
    role         = "Desarrollo de software autónomo: código, bugs, PRs en GitHub"
    version      = "3.0.0"
    capabilities = [
        "code_generation",
        "autonomous_coding",
        "bug_analysis",
        "code_review",
        "refactoring",
        "rag_retrieval",
        "web_search",
        "github_read",
        "github_write",
        "create_pull_request",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        mode = task.get("mode", "conversational").lower()
        if mode == "autonomous":
            return await self._autonomous_pipeline(task)
        return await self._conversational(task)

    # ── Modo conversacional ───────────────────────────────────────────────────

    async def _conversational(self, task: Dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_email", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name, error="query vacía")

        rag_context = await retrieve_rag_context(user_email, query, k=4, agent_name=self.name)
        prompt      = build_prompt(
            _SYSTEM_CONVERSATIONAL, query, rag_context,
            context_header="## Contexto del proyecto",
            question_header="## Tarea",
        )
        try:
            response = await invoke_llm(prompt, self.context, self.name)
            return AgentResult(
                success=True,
                output={"response": response, "source": "gabriel"},
                agent_name=self.name,
                metadata={"rag_used": bool(rag_context)},
            )
        except Exception as exc:
            logger.error(f"[gabriel] LLM error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── Modo autónomo ─────────────────────────────────────────────────────────

    async def _autonomous_pipeline(self, task: Dict[str, Any]) -> AgentResult:
        """
        Ciclo completo de coding autónomo:
          1. Analizar tarea → branch, commit message, PR title, target_file
          2. Leer archivo actual de GitHub
          3. Generar nuevo contenido con LLM
          4. Crear branch + commit + PR
          5. Notificar por WhatsApp
        """
        from config.settings import settings

        query        = task.get("query", "").strip()
        user_email   = task.get("user_email", "")
        owner        = task.get("github_owner", "") or settings.github_default_owner
        repo         = task.get("github_repo", "")  or settings.github_default_repo
        base_branch  = task.get("base_branch", settings.github_default_branch) or "main"
        hint_file    = task.get("target_file", "").strip()

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name, error="query vacía")
        if not owner or not repo:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="github_owner y github_repo son requeridos (o configurar GABRIEL_GITHUB_OWNER/GABRIEL_GITHUB_REPO)",
            )

        t0 = time.monotonic()
        logger.info(f"[gabriel] Iniciando pipeline autónomo: repo={owner}/{repo} query={query[:60]!r}")
        await self._notify(f"🛠️ *Gabriel iniciando tarea autónoma*\nRepo: `{owner}/{repo}`\nTarea: {query[:120]}")

        # ── 1. Analizar tarea ─────────────────────────────────────────────────
        analysis = await self._analyze_task(query, owner, repo, base_branch, hint_file)
        if not analysis:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="LLM no pudo analizar la tarea (respuesta no válida como JSON)",
            )

        target_file    = analysis["target_file"]
        branch_name    = analysis["branch_name"]
        commit_message = analysis["commit_message"]
        pr_title       = analysis["pr_title"]
        pr_body        = analysis["pr_body"]

        logger.info(f"[gabriel] Análisis: file={target_file} branch={branch_name}")
        await self._notify(
            f"🔍 *Gabriel analizó la tarea*\n"
            f"• Archivo objetivo: `{target_file}`\n"
            f"• Rama: `{branch_name}`\n"
            f"• PR: _{pr_title}_"
        )

        # ── 2. Leer archivo actual ────────────────────────────────────────────
        github = self._get_github_tool()
        if not github:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="GitHubTool no disponible (GITHUB_TOKEN no configurado)",
            )

        from tools.github.tool import GetFileContentsInput
        read_result = await github.get_file_contents(
            GetFileContentsInput(owner=owner, repo=repo, path=target_file, ref=base_branch)
        )
        if not read_result.success:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"No se pudo leer {target_file}: {read_result.error}",
            )

        current_content = read_result.data["content"]
        current_sha     = read_result.data["sha"]
        logger.info(f"[gabriel] Archivo leído: {target_file} ({len(current_content)} chars, sha={current_sha[:8]})")

        # ── 3. Generar nuevo contenido ────────────────────────────────────────
        new_content = await self._generate_fix(query, target_file, current_content)
        if not new_content:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="LLM no generó contenido válido para el archivo",
            )

        if new_content.strip() == current_content.strip():
            await self._notify(f"ℹ️ *Gabriel*: No hubo cambios en `{target_file}`. Tarea completada sin PR.")
            return AgentResult(
                success=True,
                output={"status": "no_changes", "file": target_file},
                agent_name=self.name,
            )

        logger.info(f"[gabriel] Nuevo contenido generado: {len(new_content)} chars")

        # ── 4. Crear branch ───────────────────────────────────────────────────
        from tools.github.tool import CreateBranchInput, CreateCommitInput, CreatePullRequestInput

        branch_result = await github.create_branch(
            CreateBranchInput(owner=owner, repo=repo, branch=branch_name, from_ref=base_branch)
        )
        if not branch_result.success:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"No se pudo crear la rama '{branch_name}': {branch_result.error}",
            )
        logger.info(f"[gabriel] Rama creada: {branch_name}")

        # ── 5. Commitear cambio ───────────────────────────────────────────────
        commit_result = await github.create_commit(
            CreateCommitInput(
                owner=owner, repo=repo,
                path=target_file,
                content=new_content,
                message=commit_message,
                branch=branch_name,
                sha=current_sha,
            )
        )
        if not commit_result.success:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"Commit falló: {commit_result.error}",
            )
        commit_sha = commit_result.data["commit_sha"]
        logger.info(f"[gabriel] Commit creado: {commit_sha[:8]}")

        # ── 6. Abrir PR ───────────────────────────────────────────────────────
        pr_result = await github.create_pull_request(
            CreatePullRequestInput(
                owner=owner, repo=repo,
                title=pr_title,
                body=pr_body + f"\n\n---\n🤖 *Generado por Gabriel* — commit `{commit_sha[:8]}`",
                head=branch_name,
                base=base_branch,
            )
        )
        if not pr_result.success:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"PR falló: {pr_result.error}",
            )

        pr_number = pr_result.data["number"]
        pr_url    = pr_result.data["url"]
        elapsed   = round(time.monotonic() - t0, 1)

        logger.info(f"[gabriel] PR #{pr_number} abierto: {pr_url} ({elapsed}s)")
        await self._notify(
            f"✅ *Gabriel completó tarea autónoma* ({elapsed}s)\n"
            f"• Archivo: `{target_file}`\n"
            f"• Commit: `{commit_sha[:8]}`\n"
            f"• PR #{pr_number}: {pr_url}"
        )

        return AgentResult(
            success=True,
            output={
                "pr_number":     pr_number,
                "pr_url":        pr_url,
                "branch":        branch_name,
                "target_file":   target_file,
                "commit_sha":    commit_sha[:8],
                "elapsed_s":     elapsed,
            },
            agent_name=self.name,
            metadata={"owner": owner, "repo": repo, "mode": "autonomous"},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _analyze_task(
        self,
        query: str,
        owner: str,
        repo: str,
        base_branch: str,
        hint_file: str,
    ) -> Optional[Dict[str, str]]:
        """
        Llama al LLM para determinar archivo objetivo, branch, commit msg y PR title.
        Retorna el dict parseado o None si el JSON no es válido.
        """
        hint = f"\nEl archivo a modificar ya está indicado por el usuario: `{hint_file}`" if hint_file else ""
        prompt = build_prompt(
            _SYSTEM_ANALYZE,
            f"Repositorio: {owner}/{repo} (rama base: {base_branch})\n{hint}\n\nTarea:\n{query}",
        )
        try:
            raw = await invoke_llm(prompt, self.context, self.name)
            # Extraer JSON aunque el LLM agregue texto alrededor
            m = re.search(r'\{[\s\S]+\}', raw)
            if not m:
                logger.error(f"[gabriel] _analyze_task: no se encontró JSON en respuesta: {raw[:200]}")
                return None
            data = json.loads(m.group(0))
            # Validar campos obligatorios
            for key in ("target_file", "branch_name", "commit_message", "pr_title", "pr_body"):
                if key not in data:
                    logger.error(f"[gabriel] _analyze_task: falta campo '{key}'")
                    return None
            # Usar hint_file si el LLM devolvió algo diferente y se proporcionó pista
            if hint_file:
                data["target_file"] = hint_file
            return data
        except (json.JSONDecodeError, Exception) as exc:
            logger.error(f"[gabriel] _analyze_task error: {exc}")
            return None

    async def _generate_fix(
        self,
        query: str,
        file_path: str,
        current_content: str,
    ) -> Optional[str]:
        """
        Genera el contenido completo del archivo modificado.
        Extrae el contenido entre los marcadores ---FILE_START--- / ---FILE_END---.
        """
        prompt = build_prompt(
            _SYSTEM_GENERATE,
            f"## Archivo: {file_path}\n\n## Contenido actual\n{current_content}\n\n## Tarea\n{query}",
        )
        try:
            raw = await invoke_llm(prompt, self.context, self.name)
            # Extraer contenido entre marcadores
            m = re.search(r'---FILE_START---\s*([\s\S]+?)\s*---FILE_END---', raw)
            if m:
                return m.group(1)
            # Fallback: si no hay marcadores, usar toda la respuesta como código
            # (el LLM a veces ignora los marcadores pero igual da código puro)
            logger.warning(f"[gabriel] _generate_fix: marcadores no encontrados, usando respuesta completa")
            return raw.strip()
        except Exception as exc:
            logger.error(f"[gabriel] _generate_fix error: {exc}")
            return None

    def _get_github_tool(self):
        """Obtiene GitHubTool desde el ToolRegistry o lo instancia directamente."""
        try:
            from tools.registry import ToolRegistry
            tool = ToolRegistry.get_or_none("github")
            if tool:
                return tool
        except Exception:
            pass
        try:
            from tools.github.tool import GitHubTool
            return GitHubTool()
        except Exception as exc:
            logger.error(f"[gabriel] GitHubTool no disponible: {exc}")
            return None

    async def _notify(self, message: str) -> None:
        """Envía una notificación WhatsApp de forma no bloqueante (fire-and-forget)."""
        try:
            import os
            import requests
            bridge_url = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
            phone      = os.environ.get("ADMIN_PHONE", "")
            if not phone or not bridge_url:
                return
            await asyncio.to_thread(
                requests.post,
                f"{bridge_url}/send",
                json={"phoneNumber": phone, "text": message},
                timeout=5,
            )
        except Exception as exc:
            logger.debug(f"[gabriel] notify falló (no crítico): {exc}")
