"""
Camael — Agente de DevOps: CI/CD, Kubernetes, GitOps y operaciones de entrega.

Angelología: Camael (כַּמָאֵל) es el ángel de la fuerza, el coraje y la ejecución.

Diferencia clave:
  Raphael → observa, detecta y sana (autonomía, 60s loop)
  Camael  → ejecuta lo que el usuario ordena (deploy, scale, gitops fixes)

Capacidades:
  k8s_status          — estado de deployments en un namespace
  k8s_scale           — escala un deployment a N réplicas
  k8s_rollout         — ejecuta rollout restart de un deployment
  pipeline_list       — lista pipelines recientes de un repo Bitbucket
  pipeline_trigger    — dispara un pipeline en Bitbucket
  pipeline_status     — estado de un pipeline por UUID
  gitops_fix          — lee YAML, aplica patch, crea branch + PR en Bitbucket
  gitops_approve      — aprueba y mergea un PR pendiente (flujo human-in-the-loop)
  (default)           — modo conversacional con RAG (detecta APROBAR/RECHAZAR)

Human-in-the-loop:
  Cuando gitops_fix crea un PR, guarda la info en Redis bajo:
    bb:pending_pr:{incident_key}
  El usuario responde "APROBAR" o "RECHAZAR" en el chat.
  El modo conversacional detecta estas palabras y ejecuta gitops_approve.

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("camael", ctx)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from agents.base.agent_registry import AgentRegistry
from agents.base.llm_utils import build_prompt, invoke_llm, retrieve_rag_context
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.camael")

_BB_WORKSPACE = os.environ.get("BITBUCKET_WORKSPACE", "amael_agenticia")
_BB_DEFAULT_REPO = os.environ.get("BITBUCKET_DEFAULT_REPO", "amael-agentic-backend")
_REDIS_PR_TTL = 7200  # 2 horas para aprobar (suficiente margen para demo y operación normal)

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_CONVERSACIONAL = """\
Eres Camael, experto en DevOps, CI/CD y GitOps con Bitbucket y Kubernetes.

Stack del proyecto:
- Orquestación: MicroK8s single-node, namespace amael-ia
- CI/CD: Bitbucket Pipelines (workspace: amael_agenticia)
- GitOps: ArgoCD observa el repo amael-agentic-backend, path k8s/, branch main
- Registry: registry.richardx.dev (privado, TLS)
- Monitoreo: Prometheus + Grafana + Tempo (OTel)

Directrices:
- Incluye comandos kubectl/docker/bb exactos cuando sea útil
- Explica el impacto de cada operación antes de ejecutarla
- Alerta sobre operaciones destructivas o de alto riesgo
- Si el usuario escribe APROBAR o RECHAZAR, responde que lo estás procesando
- Responde en el mismo idioma de la pregunta"""


# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class CamaelAgent(BaseAgent):
    """
    Camael — Agente de DevOps, GitOps y operaciones de entrega.

    task dict:
      {"task": "k8s_status",       "namespace": str}
      {"task": "k8s_scale",        "deployment": str, "replicas": int, "namespace": str}
      {"task": "k8s_rollout",      "deployment": str, "namespace": str}
      {"task": "pipeline_list",    "repo": str}
      {"task": "pipeline_trigger", "repo": str, "branch": str, "selector": str}
      {"task": "pipeline_status",  "repo": str, "pipeline_uuid": str}
      {"task": "gitops_fix",       "incident_key": str, "issue_type": str,
                                   "resource_name": str, "namespace": str,
                                   "details": str, "repo": str}
      {"task": "gitops_approve",   "incident_key": str, "action": "APROBAR|RECHAZAR"}
      {"query": str}               → modo conversacional (detecta APROBAR/RECHAZAR)
    """

    name         = "camael"
    role         = "DevOps: CI/CD, Kubernetes, GitOps y operaciones de entrega"
    version      = "2.0.0"
    capabilities = [
        "k8s_operations",
        "bitbucket_cicd",
        "gitops_fix",
        "deployment_automation",
        "rag_retrieval",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        task_type = task.get("task", "").lower()
        if task_type == "k8s_status":
            return await self._k8s_status(task)
        if task_type == "k8s_scale":
            return await self._k8s_scale(task)
        if task_type == "k8s_rollout":
            return await self._k8s_rollout(task)
        if task_type == "pipeline_list":
            return await self._pipeline_list(task)
        if task_type == "pipeline_trigger":
            return await self._pipeline_trigger(task)
        if task_type == "pipeline_status":
            return await self._pipeline_status(task)
        if task_type == "gitops_fix":
            return await self._gitops_fix(task)
        if task_type == "gitops_approve":
            return await self._gitops_approve(task)
        return await self._conversacional(task)

    # ── Conversacional ────────────────────────────────────────────────────────

    async def _conversacional(self, task: dict[str, Any]) -> AgentResult:
        query      = task.get("query", "").strip()
        user_email = task.get("user_id", "")

        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="query vacía")

        # Detectar APROBAR / RECHAZAR para el flujo human-in-the-loop
        approval = _detect_approval(query)
        if approval:
            return await self._handle_approval_keyword(approval, user_email)

        rag_ctx = await retrieve_rag_context(user_email, query, k=3, agent_name=self.name)
        prompt  = build_prompt(
            _SYSTEM_CONVERSACIONAL, query, rag_ctx,
            context_header="## Contexto de infraestructura",
            question_header="## Pregunta DevOps",
        )
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
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    async def _handle_approval_keyword(
        self, action: str, user_email: str
    ) -> AgentResult:
        """
        Detecta APROBAR/RECHAZAR en el chat y ejecuta gitops_approve
        buscando el PR pendiente más reciente en Redis.
        """
        incident_key = _get_latest_pending_pr_key()
        if not incident_key:
            return AgentResult(
                success=True,
                output={
                    "response": (
                        "No encontré ningún PR pendiente de aprobación. "
                        "Puede que haya expirado (10 min) o ya fue procesado."
                    ),
                    "source": "camael",
                },
                agent_name=self.name,
            )
        return await self._gitops_approve(
            {"incident_key": incident_key, "action": action, "user_id": user_email}
        )

    # ── K8s operations ────────────────────────────────────────────────────────

    async def _k8s_status(self, task: dict[str, Any]) -> AgentResult:
        namespace = task.get("namespace", "amael-ia")
        query = (
            f"Dame el estado de todos los deployments en el namespace {namespace}: "
            "pods running/pending/failed, imágenes actuales y últimos eventos."
        )
        return await self._k8s_query(query, task.get("user_id", ""),
                                     "k8s_status", {"namespace": namespace})

    async def _k8s_scale(self, task: dict[str, Any]) -> AgentResult:
        deployment = task.get("deployment", "")
        replicas   = int(task.get("replicas", 1))
        namespace  = task.get("namespace", "amael-ia")
        if not deployment:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'deployment' es requerido")
        query = (
            f"Escala el deployment '{deployment}' en namespace '{namespace}' "
            f"a {replicas} réplicas. Confirma el resultado."
        )
        return await self._k8s_query(query, task.get("user_id", ""),
                                     "k8s_scale",
                                     {"deployment": deployment, "replicas": replicas,
                                      "namespace": namespace})

    async def _k8s_rollout(self, task: dict[str, Any]) -> AgentResult:
        deployment = task.get("deployment", "")
        namespace  = task.get("namespace", "amael-ia")
        if not deployment:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'deployment' es requerido")
        query = (
            f"Ejecuta rollout restart del deployment '{deployment}' "
            f"en namespace '{namespace}' y reporta el estado."
        )
        return await self._k8s_query(query, task.get("user_id", ""),
                                     "k8s_rollout",
                                     {"deployment": deployment, "namespace": namespace})

    async def _k8s_query(
        self, query: str, user_email: str, task_name: str, metadata: dict
    ) -> AgentResult:
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
                data = resp.json()
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
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    # ── Bitbucket Pipelines ───────────────────────────────────────────────────

    async def _pipeline_list(self, task: dict[str, Any]) -> AgentResult:
        from agents.devops.bitbucket_client import list_pipelines
        workspace = task.get("workspace", _BB_WORKSPACE)
        repo      = task.get("repo", _BB_DEFAULT_REPO)
        try:
            pipelines = await list_pipelines(workspace, repo, limit=5)
            lines = ["Pipelines recientes:\n"]
            for p in pipelines:
                status_icon = "✅" if p["result"] == "SUCCESSFUL" else (
                    "❌" if p["result"] == "FAILED" else "🔄"
                )
                lines.append(
                    f"{status_icon} #{p['build_number']} | {p['branch']} | "
                    f"{p['status']} {p['result']} | {p['created_on'][:16]}"
                )
            response = "\n".join(lines)
            return AgentResult(
                success=True,
                output={"response": response, "pipelines": pipelines,
                        "task": "pipeline_list"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] pipeline_list error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    async def _pipeline_trigger(self, task: dict[str, Any]) -> AgentResult:
        from agents.devops.bitbucket_client import trigger_pipeline
        workspace = task.get("workspace", _BB_WORKSPACE)
        repo      = task.get("repo", _BB_DEFAULT_REPO)
        branch    = task.get("branch", "main")
        selector  = task.get("selector")     # pipeline custom name, opcional
        try:
            data = await trigger_pipeline(workspace, repo, branch, selector)
            pipeline_uuid = data.get("uuid", "")
            build_number  = data.get("build_number", "")
            response = (
                f"✅ Pipeline disparado en {workspace}/{repo}\n"
                f"Branch: {branch} | Build: #{build_number}\n"
                f"UUID: {pipeline_uuid}\n"
                f"URL: https://bitbucket.org/{workspace}/{repo}/pipelines"
            )
            logger.info(f"[camael] Pipeline triggered: {pipeline_uuid}")
            return AgentResult(
                success=True,
                output={
                    "response": response,
                    "pipeline_uuid": pipeline_uuid,
                    "build_number": build_number,
                    "task": "pipeline_trigger",
                },
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] pipeline_trigger error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    async def _pipeline_status(self, task: dict[str, Any]) -> AgentResult:
        from agents.devops.bitbucket_client import get_pipeline
        workspace     = task.get("workspace", _BB_WORKSPACE)
        repo          = task.get("repo", _BB_DEFAULT_REPO)
        pipeline_uuid = task.get("pipeline_uuid", "")
        if not pipeline_uuid:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="'pipeline_uuid' es requerido")
        try:
            data  = await get_pipeline(workspace, repo, pipeline_uuid)
            state = data.get("state", {})
            status = state.get("name", "UNKNOWN")
            result = (state.get("result") or {}).get("name", "")
            icon   = "✅" if result == "SUCCESSFUL" else ("❌" if result == "FAILED" else "🔄")
            response = (
                f"{icon} Pipeline #{data.get('build_number', '?')}\n"
                f"Estado: {status} | Resultado: {result or 'en progreso'}\n"
                f"Branch: {(data.get('target') or {}).get('ref_name', '')}"
            )
            return AgentResult(
                success=True,
                output={"response": response, "status": status,
                        "result": result, "task": "pipeline_status"},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] pipeline_status error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    # ── GitOps Fix ────────────────────────────────────────────────────────────

    async def _gitops_fix(self, task: dict[str, Any]) -> AgentResult:
        """
        Flujo GitOps completo:
        1. Lee el YAML actual de Bitbucket
        2. Aplica el patch definido en BugLibrary
        3. Crea branch + commit en Bitbucket
        4. Crea Pull Request
        5. Guarda en Redis (pending approval, TTL 10min)
        6. Notifica por WhatsApp con instrucciones de aprobación

        El merge lo ejecuta gitops_approve cuando el humano responde APROBAR.
        """
        from agents.devops import bitbucket_client as bb
        from agents.sre.bug_library import get_fix

        incident_key  = task.get("incident_key", "unknown")
        issue_type    = task.get("issue_type", "")
        resource_name = task.get("resource_name", "amael-agentic-backend")
        namespace     = task.get("namespace", "amael-ia")
        details       = task.get("details", "")
        workspace     = task.get("workspace", _BB_WORKSPACE)
        repo          = task.get("repo", _BB_DEFAULT_REPO)

        fix = get_fix(issue_type, resource_name)
        if not fix:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error=f"No hay fix automático para issue_type='{issue_type}'",
            )

        # Dedup guard: si ya existe un PR pendiente para este incident_key, no crear otro
        try:
            import json as _json
            from storage.redis.client import get_client as _get_redis
            _redis = _get_redis()
            _existing_raw = _redis.get(f"bb:pending_pr:{incident_key}")
            if _existing_raw:
                if isinstance(_existing_raw, bytes):
                    _existing_raw = _existing_raw.decode()
                _existing = _json.loads(_existing_raw)
                logger.warning(
                    f"[camael] PR ya existe para incident={incident_key} "
                    f"(PR #{_existing.get('pr_id')}) — skipping duplicate"
                )
                return AgentResult(
                    success=True,
                    output={
                        "response": f"PR #{_existing.get('pr_id')} ya existe para este incidente.",
                        "pr_id": _existing.get("pr_id"),
                        "pr_url": _existing.get("pr_url"),
                        "task": "gitops_fix",
                        "skipped_duplicate": True,
                    },
                    agent_name=self.name,
                )
        except Exception as _dedup_exc:
            logger.debug(f"[camael] dedup check error (ignorado): {_dedup_exc}")

        try:
            # 1. Leer YAML actual
            logger.info(f"[camael] GitOps fix — leyendo {fix.file_path} de {repo}")
            yaml_content = await bb.read_file(workspace, repo, fix.file_path, "main")

            # 2. Aplicar patch
            patched_content = fix.patch_fn(yaml_content)
            if patched_content == yaml_content:
                logger.warning("[camael] El patch no modificó el archivo — revisar regex")

            # 3. Crear branch
            branch_name = f"{fix.branch_prefix}-{datetime.now().strftime('%m%d-%H%M%S')}"
            logger.info(f"[camael] Creando branch '{branch_name}'")
            await bb.create_branch(workspace, repo, branch_name, from_branch="main")

            # 4. Commit del archivo parchado
            commit_message = (
                f"{fix.pr_title}\n\n"
                f"Incidente: {incident_key}\n"
                f"Recurso: {namespace}/{resource_name}\n"
                f"Generado por: Camael (DevOps Agent)"
            )
            logger.info(f"[camael] Commiteando {fix.file_path} en '{branch_name}'")
            await bb.commit_file(
                workspace, repo, fix.file_path, patched_content,
                branch=branch_name, message=commit_message,
                author="Camael DevOps Agent <camael@amael-ia.richardx.dev>",
            )

            # 5. Crear Pull Request
            pr_body = fix.pr_body_tpl.format(
                namespace=namespace,
                resource=resource_name,
                incident_key=incident_key,
                details=details[:300],
            )
            logger.info(f"[camael] Creando PR '{fix.pr_title}'")
            pr_data = await bb.create_pr(
                workspace, repo,
                title=fix.pr_title,
                description=pr_body,
                source_branch=branch_name,
                dest_branch="main",
            )
            pr_id  = pr_data.get("id", 0)
            pr_url = (pr_data.get("links") or {}).get("html", {}).get("href", "")

            # 6. Crear RFC en ServiceNow (ITIL v4 Emergency Change)
            from agents.devops import servicenow_client as sn
            from agents.devops.rfc_templates import build_emergency_rfc
            rfc_info = {"sys_id": "", "number": "N/A", "url": ""}
            if sn.is_configured():
                try:
                    rfc_payload = build_emergency_rfc(
                        issue_type   = issue_type,
                        pod_name     = resource_name,
                        namespace    = namespace,
                        incident_key = incident_key,
                        fix_summary  = fix.pr_title,
                        branch_name  = branch_name,
                        pr_url       = pr_url,
                        pr_id        = pr_id,
                        confidence   = task.get("confidence", 0.0),
                        detected_at  = task.get("detected_at", ""),
                    )
                    rfc_info = await sn.create_rfc(rfc_payload)
                    # Actualizar RFC con link al PR de Bitbucket
                    if rfc_info["sys_id"]:
                        await sn.add_work_note(
                            rfc_info["sys_id"],
                            f"Pull Request creado en Bitbucket:\n"
                            f"• PR #{pr_id}: {pr_url}\n"
                            f"• Branch: {branch_name}\n"
                            f"• Esperando aprobación del operador.",
                        )
                    # Persistir en Redis para que el verificador SRE pueda cerrar el RFC
                    if rfc_info["sys_id"]:
                        import json as _json
                        from storage.redis.client import get_client as _redis
                        _redis().setex(
                            f"sn:rfc:{incident_key}",
                            3600,  # 1 hora — suficiente para verificación post-deploy
                            _json.dumps(rfc_info),
                        )
                    logger.info(f"[camael] RFC creado: {rfc_info['number']} — {rfc_info['url']}")
                except Exception as exc_sn:
                    logger.warning(f"[camael] ServiceNow RFC falló (no crítico): {exc_sn}")

            # 7. Guardar en Redis (pending approval) — incluye RFC info
            _save_pending_pr(incident_key, {
                "pr_id":      pr_id,
                "pr_url":     pr_url,
                "repo":       repo,
                "workspace":  workspace,
                "branch":     branch_name,
                "issue_type": issue_type,
                "rfc_sys_id": rfc_info["sys_id"],
                "rfc_number": rfc_info["number"],
                "rfc_url":    rfc_info["url"],
            })

            # 8. Notificar por WhatsApp — incluye link al RFC
            rfc_line = (
                f"🎫 RFC: {rfc_info['number']} — {rfc_info['url']}\n"
                if rfc_info["number"] not in ("N/A", "ERROR", "")
                else ""
            )
            wa_msg = (
                f"🔧 *PR LISTO PARA REVISIÓN*\n"
                f"Incidente: {incident_key}\n"
                f"Fix: {fix.pr_title}\n"
                f"PR #{pr_id}: {pr_url}\n"
                f"{rfc_line}"
                f"\nResponde *APROBAR* para mergear a main y que ArgoCD despliegue.\n"
                f"Responde *RECHAZAR* para cancelar.\n"
                f"_(Expira en 10 minutos)_"
            )
            _notify_whatsapp(wa_msg)

            response = (
                f"✅ GitOps fix iniciado para {issue_type}\n"
                f"Branch: {branch_name}\n"
                f"PR #{pr_id} creado: {pr_url}\n"
                + (f"RFC: {rfc_info['number']} ({rfc_info['url']})\n" if rfc_info["sys_id"] else "")
                + "Esperando aprobación — escribe APROBAR o RECHAZAR."
            )
            logger.info(f"[camael] GitOps fix completo — PR #{pr_id} pendiente de aprobación")
            from observability.metrics import GITOPS_PR_CREATED_TOTAL
            GITOPS_PR_CREATED_TOTAL.labels(issue_type=issue_type).inc()
            return AgentResult(
                success=True,
                output={
                    "response": response,
                    "pr_id": pr_id,
                    "pr_url": pr_url,
                    "branch": branch_name,
                    "incident_key": incident_key,
                    "task": "gitops_fix",
                },
                agent_name=self.name,
            )

        except Exception as exc:
            logger.error(f"[camael] gitops_fix error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))

    # ── GitOps Approve ────────────────────────────────────────────────────────

    async def _gitops_approve(self, task: dict[str, Any]) -> AgentResult:
        """
        Aprueba y mergea (APROBAR) o cancela (RECHAZAR) un PR pendiente.
        Lee la info del PR desde Redis usando el incident_key.
        """
        from agents.devops import bitbucket_client as bb

        incident_key = task.get("incident_key", "")
        action       = task.get("action", "").upper().strip()

        if action not in ("APROBAR", "RECHAZAR"):
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="action debe ser 'APROBAR' o 'RECHAZAR'",
            )

        pr_info = _get_pending_pr(incident_key)
        if not pr_info:
            return AgentResult(
                success=True,
                output={
                    "response": (
                        "No encontré el PR pendiente para ese incidente. "
                        "Puede que haya expirado (10 min) o ya fue procesado."
                    ),
                    "source": "camael",
                },
                agent_name=self.name,
            )

        workspace = pr_info["workspace"]
        repo      = pr_info["repo"]
        pr_id     = int(pr_info["pr_id"])
        pr_url    = pr_info.get("pr_url", "")

        if action == "RECHAZAR":
            _delete_pending_pr(incident_key)
            from observability.metrics import GITOPS_PR_REJECTED_TOTAL
            GITOPS_PR_REJECTED_TOTAL.labels(issue_type=pr_info.get("issue_type", "unknown")).inc()
            # Cancelar RFC en ServiceNow
            rfc_sys_id = pr_info.get("rfc_sys_id", "")
            rfc_number = pr_info.get("rfc_number", "N/A")
            if rfc_sys_id:
                try:
                    from agents.devops import servicenow_client as sn
                    await sn.update_rfc(rfc_sys_id, {
                        "state":       sn.RFCState.CANCELLED,
                        "work_notes":  f"PR #{pr_id} rechazado por el operador. Fix cancelado.",
                        "close_notes": "Cambio cancelado por el operador vía WhatsApp/chat.",
                    })
                    logger.info(f"[camael] RFC {rfc_number} cancelado en ServiceNow")
                except Exception as exc_sn:
                    logger.warning(f"[camael] SN cancel RFC falló: {exc_sn}")
            _notify_whatsapp(
                f"❌ PR #{pr_id} cancelado por el operador.\n"
                f"El fix para {incident_key} no será desplegado."
                + (f"\n🎫 RFC {rfc_number} cancelado en ServiceNow." if rfc_sys_id else "")
            )
            return AgentResult(
                success=True,
                output={
                    "response": f"PR #{pr_id} cancelado. No se realizarán cambios.",
                    "task": "gitops_approve",
                    "action": "RECHAZAR",
                },
                agent_name=self.name,
            )

        # APROBAR → merge directo (token del agente es el mismo autor, Bitbucket
        # permite self-merge en repos sin branch restrictions configuradas)
        rfc_sys_id = pr_info.get("rfc_sys_id", "")
        rfc_number = pr_info.get("rfc_number", "N/A")
        rfc_url    = pr_info.get("rfc_url", "")
        try:
            logger.info(f"[camael] Mergeando PR #{pr_id} en {workspace}/{repo}")
            merge_result = await bb.merge_pr(
                workspace, repo, pr_id,
                message=f"Merge aprobado por operador — Incidente {incident_key}",
                merge_strategy="merge_commit",
            )
            _delete_pending_pr(incident_key)

            merge_hash = (merge_result.get("merge_commit") or {}).get("hash", "")[:8]

            # Actualizar RFC → estado Implement
            if rfc_sys_id:
                try:
                    from agents.devops import servicenow_client as sn
                    await sn.update_rfc(rfc_sys_id, {
                        "state":      sn.RFCState.IMPLEMENT,
                        "work_notes": (
                            f"PR #{pr_id} aprobado y mergeado a main por el operador.\n"
                            f"Commit: {merge_hash}\n"
                            f"ArgoCD sincronizando cambios al cluster..."
                        ),
                    })
                    logger.info(f"[camael] RFC {rfc_number} → Implement en ServiceNow")
                except Exception as exc_sn:
                    logger.warning(f"[camael] SN update RFC falló: {exc_sn}")

            rfc_line = (
                f"🎫 RFC {rfc_number} → Implement\n"
                if rfc_sys_id else ""
            )
            response = (
                f"✅ PR #{pr_id} mergeado a main\n"
                f"Commit: {merge_hash}\n"
                + rfc_line
                + "ArgoCD detectará el cambio y desplegará en ~30s."
            )
            _notify_whatsapp(
                f"✅ *MERGE COMPLETADO*\n"
                f"PR #{pr_id} mergeado a main.\n"
                f"ArgoCD está sincronizando — el fix estará activo en ~30s.\n"
                f"Incidente: {incident_key}"
                + (f"\n🎫 RFC {rfc_number} en ServiceNow actualizado → Implement." if rfc_sys_id else "")
            )
            logger.info(f"[camael] PR #{pr_id} mergeado correctamente — {merge_hash}")
            from observability.metrics import GITOPS_PR_MERGED_TOTAL
            GITOPS_PR_MERGED_TOTAL.labels(issue_type=pr_info.get("issue_type", "unknown")).inc()
            return AgentResult(
                success=True,
                output={
                    "response": response,
                    "pr_id": pr_id,
                    "merge_commit": merge_hash,
                    "task": "gitops_approve",
                    "action": "APROBAR",
                },
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[camael] gitops_approve merge error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=str(exc))


# ── Helpers internos ──────────────────────────────────────────────────────────

def _detect_approval(query: str) -> str | None:
    """Detecta si el usuario escribió APROBAR o RECHAZAR."""
    q = query.strip().upper()
    if q in ("APROBAR", "APPROVE", "SI", "SÍ", "YES"):
        return "APROBAR"
    if q in ("RECHAZAR", "REJECT", "NO", "CANCEL", "CANCELAR"):
        return "RECHAZAR"
    return None


def _save_pending_pr(incident_key: str, pr_info: dict) -> None:
    try:
        from storage.redis import get_client
        redis = get_client()
        redis.setex(
            f"bb:pending_pr:{incident_key}",
            _REDIS_PR_TTL,
            json.dumps(pr_info),
        )
    except Exception as exc:
        logger.warning(f"[camael] No se pudo guardar PR en Redis: {exc}")


def _get_pending_pr(incident_key: str) -> dict | None:
    try:
        from storage.redis import get_client
        redis = get_client()
        raw = redis.get(f"bb:pending_pr:{incident_key}")
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning(f"[camael] No se pudo leer PR de Redis: {exc}")
        return None


def _get_latest_pending_pr_key() -> str | None:
    """Busca el primer bb:pending_pr:* disponible en Redis."""
    try:
        from storage.redis import get_client
        redis = get_client()
        keys = redis.keys("bb:pending_pr:*")
        if keys:
            key = keys[0] if isinstance(keys[0], str) else keys[0].decode()
            return key.replace("bb:pending_pr:", "")
    except Exception as exc:
        logger.warning(f"[camael] No se pudo buscar PR pendiente: {exc}")
    return None


def _delete_pending_pr(incident_key: str) -> None:
    try:
        from storage.redis import get_client
        get_client().delete(f"bb:pending_pr:{incident_key}")
    except Exception:
        pass


def _notify_whatsapp(message: str) -> None:
    """Envía notificación al WhatsApp bridge de forma best-effort."""
    import threading
    try:
        from config.settings import settings
        admin_phone = settings.admin_phone

        def _send():
            import httpx
            try:
                httpx.post(
                    f"{settings.whatsapp_bridge_url}/send",
                    json={"number": admin_phone, "message": message},
                    timeout=10.0,
                )
            except Exception as e:
                logger.warning(f"[camael] WhatsApp notify failed: {e}")

        threading.Thread(target=_send, daemon=True).start()
    except Exception as exc:
        logger.warning(f"[camael] _notify_whatsapp setup error: {exc}")
