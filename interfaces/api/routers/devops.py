"""
Router /api/devops — webhooks y operaciones DevOps (Camael).

Endpoints:
  POST /api/devops/ci-hook          — GitHub webhook para workflow_run events
                                      Notifica por WhatsApp cuando un workflow falla
                                    — pull_request events: notifica cuando se abre un PR hacia main
  POST /api/devops/webhook/bitbucket — Bitbucket webhook para pipeline failures y PR events
                                      Notifica por WhatsApp cuando un pipeline falla o un PR es rechazado
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.devops")

router = APIRouter(prefix="/api/devops", tags=["devops"])


# ── Webhook GitHub CI ─────────────────────────────────────────────────────────

@router.post("/ci-hook", status_code=status.HTTP_200_OK)
async def github_ci_hook(request: Request):
    """
    Recibe eventos `workflow_run` de GitHub Actions via webhook.

    GitHub envía este evento cuando un workflow:
      - completed (conclusion: success | failure | cancelled | skipped)
      - requested / in_progress

    Acción:
      - Si conclusion == 'failure' → Camael notifica por WhatsApp
      - Si conclusion == 'success' → log silencioso (sin spam)
      - Otros → ignorado

    Seguridad: valida la firma HMAC-SHA256 del payload si
    GITHUB_WEBHOOK_SECRET está configurado.

    Configuración en GitHub:
      Settings → Webhooks → Add webhook
        Payload URL:  https://amael-ia.richardx.dev/api/devops/ci-hook
        Content type: application/json
        Secret:       <valor de GITHUB_WEBHOOK_SECRET>
        Events:       Workflow runs
    """
    raw_body = await request.body()

    # Validar firma HMAC si el secret está configurado
    _verify_github_signature(request, raw_body)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    event_type = request.headers.get("X-GitHub-Event", "")

    if event_type == "pull_request":
        return await _handle_pull_request(payload)

    if event_type != "workflow_run":
        # Ignorar otros eventos (ping, push, etc.)
        return {"status": "ignored", "event": event_type}

    action = payload.get("action", "")
    run    = payload.get("workflow_run", {})

    if action != "completed":
        return {"status": "ignored", "action": action}

    conclusion  = run.get("conclusion", "")
    workflow    = run.get("name", "unknown")
    branch      = run.get("head_branch", "unknown")
    commit_sha  = run.get("head_sha", "")[:8]
    run_url     = run.get("html_url", "")
    actor       = run.get("triggering_actor", {}).get("login", "unknown")

    logger.info(
        f"[devops/ci-hook] workflow_run completed: workflow={workflow} "
        f"branch={branch} conclusion={conclusion} commit={commit_sha}"
    )

    if conclusion == "failure":
        msg = (
            f"❌ *CI/CD falló* en `{branch}`\n"
            f"• Workflow: `{workflow}`\n"
            f"• Commit: `{commit_sha}` (por @{actor})\n"
            f"• {run_url}"
        )
        await _notify_whatsapp(msg)
        return {"status": "notified", "conclusion": conclusion, "workflow": workflow}

    if conclusion == "success":
        logger.debug(f"[devops/ci-hook] workflow exitoso: {workflow}@{branch}")
        return {"status": "ok", "conclusion": conclusion}

    # cancelled, skipped, timed_out, action_required, neutral, stale
    logger.info(f"[devops/ci-hook] conclusion={conclusion} — sin acción")
    return {"status": "ignored", "conclusion": conclusion}


# ── Pull Request handler ──────────────────────────────────────────────────────

async def _handle_pull_request(payload: dict) -> dict:
    """
    Maneja eventos pull_request de GitHub.
    Notifica por WhatsApp cuando se abre (o re-abre) un PR cuyo base es 'main'.
    """
    action = payload.get("action", "")
    if action not in ("opened", "reopened", "ready_for_review"):
        return {"status": "ignored", "action": action}

    pr   = payload.get("pull_request", {})
    base = pr.get("base", {}).get("ref", "")
    if base != "main":
        return {"status": "ignored", "base": base}

    number  = pr.get("number", "?")
    title   = pr.get("title", "sin título")
    author  = pr.get("user", {}).get("login", "unknown")
    head    = pr.get("head", {}).get("ref", "?")
    pr_url  = pr.get("html_url", "")
    draft   = pr.get("draft", False)

    estado = "📝 *Draft PR*" if draft else "🔔 *PR listo para revisión*"
    msg = (
        f"{estado}\n"
        f"• #{number}: {title}\n"
        f"• Rama: `{head}` → `main`\n"
        f"• Autor: @{author}\n"
        f"• {pr_url}"
    )

    logger.info(f"[devops/ci-hook] PR #{number} abierto hacia main por @{author}")
    await _notify_whatsapp(msg)
    return {"status": "notified", "pr": number, "action": action}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_github_signature(request: Request, raw_body: bytes) -> None:
    """
    Valida la firma HMAC-SHA256 del webhook si GITHUB_WEBHOOK_SECRET está configurado.
    Si el secret no está configurado, acepta sin validar (útil en desarrollo).
    """
    import os
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return  # Sin secret configurado → aceptar (dev/testing)

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not sig_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la firma X-Hub-Signature-256",
        )

    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firma del webhook inválida",
        )


# ── WhatsApp /devops command dispatcher ──────────────────────────────────────

class DevOpsCommandRequest(BaseModel):
    command: str
    phone:   str | None = None


@router.post("/command", dependencies=[Depends(require_internal_secret)])
async def handle_devops_command(body: DevOpsCommandRequest) -> dict:
    """
    Dispatcher de comandos /devops desde el whatsapp-bridge.

    Comandos soportados:
        estado / pipelines  — últimos 5 pipelines en Bitbucket
        aprobar             — aprueba y mergea el PR pendiente más reciente
        rechazar            — declina el PR pendiente más reciente
        ayuda               — lista de comandos
    """
    cmd = body.command.strip().lower()
    cmd_base = cmd.split()[0] if cmd else "ayuda"
    logger.info(f"[devops/command] cmd='{cmd}' phone={body.phone}")

    try:
        if cmd_base in ("estado", "pipelines"):
            return {"reply": await _cmd_pipelines()}

        elif cmd_base == "pr":
            return {"reply": await _cmd_pr()}

        elif cmd_base == "aprobar":
            pr_id_arg = cmd.split()[1].lstrip("#") if len(cmd.split()) > 1 else None
            return {"reply": await _cmd_aprobar(pr_id_arg)}

        elif cmd_base == "rechazar":
            pr_id_arg = cmd.split()[1].lstrip("#") if len(cmd.split()) > 1 else None
            return {"reply": await _cmd_rechazar(pr_id_arg)}

        elif cmd_base == "sn":
            rfc_number = cmd.split()[1].upper() if len(cmd.split()) > 1 else None
            return {"reply": await _cmd_sn(rfc_number)}

        elif cmd_base == "ayuda":
            return {"reply": _devops_help()}

        else:
            return {"reply": f"Comando '{cmd_base}' no reconocido. Escribe */devops ayuda*"}

    except Exception as exc:
        logger.error(f"[devops/command] error: {exc}", exc_info=True)
        return {"reply": f"❌ Error: {exc}"}


async def _cmd_pr() -> str:
    """Lista todos los PRs pendientes de aprobación (desde Redis)."""
    import json as _json

    from storage.redis.client import get_client
    redis = get_client()
    keys  = redis.keys("bb:pending_pr:*")
    if not keys:
        return "No hay PRs pendientes de aprobación."

    decoded_keys = [k if isinstance(k, str) else k.decode() for k in keys]

    if len(decoded_keys) == 1:
        raw     = redis.get(decoded_keys[0])
        pr_info = _json.loads(raw) if raw else {}
        pr_id   = pr_info.get("pr_id", "?")
        branch  = pr_info.get("branch", "?")
        pr_url  = pr_info.get("pr_url", "")
        issue_type = pr_info.get("issue_type", "?")
        repo    = pr_info.get("repo", "?")
        msg = (
            f"🔔 *PR pendiente de aprobación*\n"
            f"• PR #{pr_id} · repo: `{repo}`\n"
            f"• Tipo: `{issue_type}`\n"
            f"• Rama: `{branch}` → `main`\n"
        )
        if pr_url:
            msg += f"• {pr_url}\n"
        msg += f"Responde */devops aprobar #{pr_id}* o */devops rechazar #{pr_id}*"
        return msg

    # Múltiples PRs: listar todos con su ID explícito
    msg = f"🔔 *{len(decoded_keys)} PRs pendientes de aprobación*\n\n"
    for key in sorted(decoded_keys):
        raw     = redis.get(key)
        pr_info = _json.loads(raw) if raw else {}
        pr_id   = pr_info.get("pr_id", "?")
        issue_type = pr_info.get("issue_type", "?")
        repo    = pr_info.get("repo", "?")
        branch  = pr_info.get("branch", "?")
        pr_url  = pr_info.get("pr_url", "")
        msg += f"• PR *#{pr_id}* · `{repo}` · `{issue_type}`\n"
        if pr_url:
            msg += f"  {pr_url}\n"
    msg += "\nUsa */devops aprobar #PR_ID* para aprobar uno específico.\n"
    msg += f"Ejemplo: */devops aprobar #{_json.loads(redis.get(sorted(decoded_keys)[0]) or '{}').get('pr_id', '?')}*"
    return msg


async def _cmd_pipelines() -> str:
    import os

    from agents.devops.bitbucket_client import list_pipelines
    ws   = os.environ.get("BITBUCKET_WORKSPACE", "")
    repo = os.environ.get("BITBUCKET_DEFAULT_REPO", "amael-agentic-backend")
    pipes = await list_pipelines(ws, repo, limit=5)
    if not pipes:
        return "No hay pipelines recientes en Bitbucket."
    icons = {"SUCCESSFUL": "✅", "FAILED": "❌", "STOPPED": "🛑", "INPROGRESS": "🔄"}
    lines = ["*Pipelines recientes — Bitbucket*"]
    for p in pipes:
        icon = icons.get(p.get("result") or p.get("status", ""), "⏳")
        lines.append(
            f"{icon} #{p['build_number']} `{p['branch']}` — "
            f"{p.get('result') or p.get('status', '?')}"
        )
    return "\n".join(lines)


async def _cmd_aprobar(pr_id_arg: str | None = None) -> str:
    """Aprueba y mergea un PR pendiente.

    Si pr_id_arg es None y solo hay 1 PR pendiente, lo aprueba automáticamente.
    Si hay múltiples PRs, requiere pr_id_arg explícito.
    Soporta: /devops aprobar #31  o  /devops aprobar 31
    """
    import json as _json
    import os

    from agents.devops.bitbucket_client import merge_pr
    from storage.redis.client import get_client

    redis = get_client()
    keys  = redis.keys("bb:pending_pr:*")
    if not keys:
        return "No hay PRs pendientes de aprobación."

    decoded_keys = [k if isinstance(k, str) else k.decode() for k in keys]

    # Seleccionar la clave correcta
    if pr_id_arg:
        # Buscar el PR por ID entre todos los pendientes
        target_key = None
        for key in decoded_keys:
            raw = redis.get(key)
            pr_info = _json.loads(raw) if raw else {}
            if str(pr_info.get("pr_id", "")) == str(pr_id_arg):
                target_key = key
                break
        if not target_key:
            ids = [str(_json.loads(redis.get(k) or "{}").get("pr_id", "?")) for k in decoded_keys]
            return f"❌ PR #{pr_id_arg} no encontrado entre los pendientes: {', '.join(f'#{i}' for i in ids)}"
    elif len(decoded_keys) == 1:
        target_key = decoded_keys[0]
    else:
        # Múltiples PRs sin especificar cuál: listar y pedir ID
        ids = []
        for key in sorted(decoded_keys):
            pr_info = _json.loads(redis.get(key) or "{}")
            ids.append(f"#{pr_info.get('pr_id', '?')} ({pr_info.get('repo', '?')})")
        return (
            f"⚠️ Hay {len(decoded_keys)} PRs pendientes. Especifica cuál:\n"
            + "\n".join(f"• {i}" for i in ids)
            + "\n\nEjemplo: */devops aprobar #" + ids[0].split()[0].lstrip("#") + "*"
        )

    raw     = redis.get(target_key)
    pr_info = _json.loads(raw) if raw else {}
    pr_id   = int(pr_info.get("pr_id", 0))
    ws      = pr_info.get("workspace") or os.environ.get("BITBUCKET_WORKSPACE", "")
    repo    = pr_info.get("repo") or os.environ.get("BITBUCKET_DEFAULT_REPO", "amael-agentic-backend")

    if not pr_id:
        return "❌ PR pendiente encontrado pero sin ID válido."

    try:
        await merge_pr(ws, repo, pr_id, message="Aprobado via WhatsApp /devops aprobar")
    except RuntimeError as exc:
        err = str(exc)
        # PR ya mergeado (409 Conflict) o no existe (404) — limpiar Redis igualmente
        if "409" in err or "404" in err or "already merged" in err.lower():
            redis.delete(target_key)
            return f"⚠️ PR #{pr_id} ya fue mergeado o no existe en Bitbucket. Tracking limpiado."
        raise

    redis.delete(target_key)

    from observability.metrics import GITOPS_PR_MERGED_TOTAL
    try:
        GITOPS_PR_MERGED_TOTAL.labels(issue_type=pr_info.get("issue_type", "unknown")).inc()
    except Exception:
        pass

    logger.info(f"[devops/command] PR #{pr_id} mergeado via WhatsApp (repo={repo})")
    return f"✅ PR #{pr_id} mergeado a main · repo: `{repo}`"


async def _cmd_rechazar(pr_id_arg: str | None = None) -> str:
    """Declina un PR pendiente. Misma lógica de selección que _cmd_aprobar."""
    import json as _json
    import os

    import httpx

    from agents.devops.bitbucket_client import _BB_BASE, _auth, _headers
    from storage.redis.client import get_client

    redis = get_client()
    keys  = redis.keys("bb:pending_pr:*")
    if not keys:
        return "No hay PRs pendientes."

    decoded_keys = [k if isinstance(k, str) else k.decode() for k in keys]

    if pr_id_arg:
        target_key = None
        for key in decoded_keys:
            raw = redis.get(key)
            pr_info = _json.loads(raw) if raw else {}
            if str(pr_info.get("pr_id", "")) == str(pr_id_arg):
                target_key = key
                break
        if not target_key:
            ids = [str(_json.loads(redis.get(k) or "{}").get("pr_id", "?")) for k in decoded_keys]
            return f"❌ PR #{pr_id_arg} no encontrado entre los pendientes: {', '.join(f'#{i}' for i in ids)}"
    elif len(decoded_keys) == 1:
        target_key = decoded_keys[0]
    else:
        ids = []
        for key in sorted(decoded_keys):
            pr_info = _json.loads(redis.get(key) or "{}")
            ids.append(f"#{pr_info.get('pr_id', '?')} ({pr_info.get('repo', '?')})")
        return (
            f"⚠️ Hay {len(decoded_keys)} PRs pendientes. Especifica cuál rechazar:\n"
            + "\n".join(f"• {i}" for i in ids)
            + "\n\nEjemplo: */devops rechazar #" + ids[0].split()[0].lstrip("#") + "*"
        )

    raw     = redis.get(target_key)
    pr_info = _json.loads(raw) if raw else {}
    pr_id   = int(pr_info.get("pr_id", 0))
    ws      = pr_info.get("workspace") or os.environ.get("BITBUCKET_WORKSPACE", "")
    repo    = pr_info.get("repo") or os.environ.get("BITBUCKET_DEFAULT_REPO", "amael-agentic-backend")

    if not pr_id:
        return "❌ PR pendiente encontrado pero sin ID válido."

    url = f"{_BB_BASE}/repositories/{ws}/{repo}/pullrequests/{pr_id}/decline"
    async with httpx.AsyncClient(timeout=30, auth=_auth()) as client:
        resp = await client.post(url, headers=_headers(), json={})
        # 409 = ya mergeado/declinado, 404 = no existe — limpiar Redis de todas formas
        if resp.status_code in (404, 409):
            redis.delete(target_key)
            return f"⚠️ PR #{pr_id} ya fue mergeado o declinado en Bitbucket. Tracking limpiado."
        if resp.status_code >= 400:
            return f"❌ Error al declinar PR #{pr_id}: {resp.status_code}"

    redis.delete(target_key)

    from observability.metrics import GITOPS_PR_REJECTED_TOTAL
    try:
        GITOPS_PR_REJECTED_TOTAL.labels(issue_type=pr_info.get("issue_type", "unknown")).inc()
    except Exception:
        pass

    logger.info(f"[devops/command] PR #{pr_id} rechazado via WhatsApp (repo={repo})")
    return f"🚫 PR #{pr_id} rechazado · repo: `{repo}`"


async def _cmd_sn(rfc_number: str | None = None) -> str:
    """
    Muestra el estado del RFC en ServiceNow.
    Sin argumento: usa el RFC del PR pendiente en Redis.
    Con argumento (ej. CHG0030002): busca ese RFC directamente.
    """
    import json as _json

    from agents.devops import servicenow_client as sn

    if not sn.is_configured():
        return "❌ ServiceNow no está configurado en este entorno."

    sys_id = None

    if rfc_number:
        # Buscar por número de RFC en ServiceNow
        try:
            import httpx
            base, user, pwd = sn._cfg()
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{base}/api/now/table/change_request",
                    auth=(user, pwd),
                    headers={"Accept": "application/json"},
                    params={
                        "sysparm_query": f"number={rfc_number}",
                        "sysparm_fields": "sys_id,number,state,short_description,type,work_notes",
                        "sysparm_limit": "1",
                    },
                )
                results = r.json().get("result", [])
                if not results:
                    return f"No encontré el RFC *{rfc_number}* en ServiceNow."
                sys_id = results[0].get("sys_id", "")
        except Exception as exc:
            return f"❌ Error buscando RFC: {exc}"
    else:
        # Buscar sys_id desde el PR pendiente en Redis
        from storage.redis.client import get_client
        redis = get_client()
        # Primero intentar desde sn:rfc:* (guardado por Camael)
        sn_keys = redis.keys("sn:rfc:*")
        if sn_keys:
            key = sn_keys[0] if isinstance(sn_keys[0], str) else sn_keys[0].decode()
            raw = redis.get(key)
            rfc_data = _json.loads(raw) if raw else {}
            sys_id = rfc_data.get("sys_id", "")
        else:
            # Intentar desde bb:pending_pr:*
            pr_keys = redis.keys("bb:pending_pr:*")
            if pr_keys:
                key = pr_keys[0] if isinstance(pr_keys[0], str) else pr_keys[0].decode()
                raw = redis.get(key)
                pr_info = _json.loads(raw) if raw else {}
                sys_id = pr_info.get("rfc_sys_id", "")

        if not sys_id:
            return "No hay RFCs activos en este momento.\nUsa */devops sn CHG000XXXX* para buscar uno específico."

    # Obtener datos del RFC
    rfc = await sn.get_rfc(sys_id)
    if not rfc:
        return "❌ No se pudo obtener información del RFC."

    base = sn._cfg()[0]
    return (
        f"🎫 *RFC {rfc['number']}*\n"
        f"• Estado: *{rfc['state_label']}*\n"
        f"• Tipo: {rfc.get('type', 'Emergency')}\n"
        f"• Descripción: {rfc['short_description']}\n"
        f"• Ver en ServiceNow:\n{rfc['url']}"
    )


def _devops_help() -> str:
    return (
        "*Comandos /devops disponibles:*\n"
        "• `/devops estado` — últimos pipelines en Bitbucket\n"
        "• `/devops pr` — ver PR pendiente de aprobación\n"
        "• `/devops aprobar` — mergea el PR pendiente\n"
        "• `/devops rechazar` — declina el PR pendiente\n"
        "• `/devops sn` — RFC activo en ServiceNow\n"
        "• `/devops sn CHG000X` — buscar RFC específico\n"
        "• `/devops ayuda` — esta lista"
    )


# ── Webhook Bitbucket ─────────────────────────────────────────────────────────

@router.post("/webhook/bitbucket", status_code=status.HTTP_200_OK)
async def bitbucket_webhook(request: Request):
    """
    Recibe eventos de Bitbucket via webhook.

    Eventos soportados:
      repo:commit_status_updated  — Pipeline completado (FAILED/STOPPED → WhatsApp)
      pullrequest:rejected        — PR rechazado → WhatsApp
      pullrequest:fulfilled       — PR mergeado → log silencioso

    Seguridad: valida token opcional via header X-Bitbucket-Token si
    BITBUCKET_WEBHOOK_SECRET está configurado.

    Configuración en Bitbucket:
      Repository settings → Webhooks → Add webhook
        URL:     https://amael-ia.richardx.dev/api/devops/webhook/bitbucket
        Triggers: Repository: Commit status updated
                  Pull request: Fulfilled, Rejected
    """
    import os
    await request.body()

    # Validación opcional por token
    secret = os.environ.get("BITBUCKET_WEBHOOK_SECRET", "")
    if secret:
        token = request.headers.get("X-Bitbucket-Token", "")
        if not hmac.compare_digest(token, secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Token inválido")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    event_key = request.headers.get("X-Event-Key", "")
    logger.info(f"[devops/bb-webhook] evento recibido: {event_key}")

    if event_key == "repo:commit_status_updated":
        return await _handle_bb_commit_status(payload)

    if event_key == "pullrequest:rejected":
        return await _handle_bb_pr_rejected(payload)

    if event_key == "pullrequest:fulfilled":
        return await _handle_bb_pr_fulfilled(payload)

    if event_key == "pullrequest:created":
        return await _handle_bb_pr_created(payload)

    logger.debug(f"[devops/bb-webhook] evento ignorado: {event_key}")
    return {"status": "ignored", "event": event_key}


async def _handle_bb_commit_status(payload: dict) -> dict:
    """Pipeline state change → WhatsApp notify (FAILED/STOPPED/SUCCESSFUL)."""
    commit_status = payload.get("commit_status", {})
    state = commit_status.get("state", "")       # INPROGRESS, SUCCESSFUL, FAILED, STOPPED
    repo  = payload.get("repository", {}).get("name", "unknown")
    name  = commit_status.get("name", "Pipeline")
    desc  = commit_status.get("description", "")
    url   = commit_status.get("url", "")

    if state == "SUCCESSFUL":
        msg = (
            f"✅ *Pipeline exitoso* en `{repo}`\n"
            f"• {name}\n"
            f"• ArgoCD sincronizando cambios..."
        )
        if url:
            msg += f"\n• {url}"
        logger.info(f"[devops/bb-webhook] pipeline exitoso en {repo}: {name}")
        await _notify_whatsapp(msg)
        return {"status": "notified", "state": state, "repo": repo}

    if state not in ("FAILED", "STOPPED"):
        logger.debug(f"[devops/bb-webhook] pipeline state={state} — ignorado")
        return {"status": "ignored", "state": state}

    emoji = "❌" if state == "FAILED" else "🛑"
    msg = (
        f"{emoji} *Pipeline {state}* en `{repo}`\n"
        f"• {name}\n"
    )
    if desc:
        msg += f"• {desc}\n"
    if url:
        msg += f"• {url}"

    logger.warning(f"[devops/bb-webhook] pipeline {state} en {repo}: {name}")
    await _notify_whatsapp(msg)
    return {"status": "notified", "state": state, "repo": repo}


async def _handle_bb_pr_fulfilled(payload: dict) -> dict:
    """PR mergeado → WhatsApp notify: pipeline arrancando."""
    pr     = payload.get("pullrequest", {})
    pr_id  = pr.get("id", "?")
    title  = pr.get("title", "sin título")
    branch = pr.get("source", {}).get("branch", {}).get("name", "?")
    repo   = payload.get("repository", {}).get("name", "unknown")

    msg = (
        f"✅ *PR #{pr_id} mergeado a main*\n"
        f"• {title}\n"
        f"• Rama: `{branch}` → `main`\n"
        f"• Pipeline arrancando en `{repo}`..."
    )
    logger.info(f"[devops/bb-webhook] PR #{pr_id} mergeado — notificando")
    await _notify_whatsapp(msg)
    return {"status": "notified", "event": "pullrequest:fulfilled", "pr": pr_id}


async def _handle_bb_pr_created(payload: dict) -> dict:
    """PR creado (por Camael) → WhatsApp notify con instrucciones de aprobación."""
    pr     = payload.get("pullrequest", {})
    pr_id  = pr.get("id", "?")
    title  = pr.get("title", "sin título")
    author = pr.get("author", {}).get("display_name", "unknown")
    branch = pr.get("source", {}).get("branch", {}).get("name", "?")
    pr_url = pr.get("links", {}).get("html", {}).get("href", "")

    msg = (
        f"🔔 *PR creado en Bitbucket*\n"
        f"• #{pr_id}: {title}\n"
        f"• Rama: `{branch}` → `main`\n"
        f"• Autor: {author}\n"
    )
    if pr_url:
        msg += f"• {pr_url}\n"
    msg += "Responde */devops aprobar* o */devops rechazar*"

    logger.info(f"[devops/bb-webhook] PR #{pr_id} creado por {author}")
    await _notify_whatsapp(msg)
    return {"status": "notified", "event": "pullrequest:created", "pr": pr_id}


async def _handle_bb_pr_rejected(payload: dict) -> dict:
    """PR rechazado → WhatsApp notify."""
    pr     = payload.get("pullrequest", {})
    pr_id  = pr.get("id", "?")
    title  = pr.get("title", "sin título")
    reason = pr.get("reason", "")
    branch = pr.get("source", {}).get("branch", {}).get("name", "?")
    author = pr.get("author", {}).get("display_name", "unknown")
    pr_url = pr.get("links", {}).get("html", {}).get("href", "")

    msg = (
        f"🚫 *PR rechazado* en Bitbucket\n"
        f"• #{pr_id}: {title}\n"
        f"• Rama: `{branch}`\n"
        f"• Autor: {author}\n"
    )
    if reason:
        msg += f"• Razón: {reason}\n"
    if pr_url:
        msg += f"• {pr_url}"

    logger.info(f"[devops/bb-webhook] PR #{pr_id} rechazado: {title}")
    await _notify_whatsapp(msg)
    return {"status": "notified", "pr": pr_id}


async def _notify_whatsapp(message: str) -> None:
    """Envía notificación WhatsApp vía el bridge. Fire-and-forget."""
    import asyncio
    import os

    import requests as _req

    bridge_url = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
    phone      = os.environ.get("ADMIN_PHONE", "")

    if not phone:
        logger.warning("[devops/ci-hook] ADMIN_PHONE no configurado — notificación omitida")
        return

    try:
        await asyncio.to_thread(
            _req.post,
            f"{bridge_url}/send",
            json={"phoneNumber": phone, "text": message},
            timeout=5,
        )
        logger.info(f"[devops/ci-hook] WhatsApp notificado: {phone}")
    except Exception as exc:
        logger.debug(f"[devops/ci-hook] notify falló (no crítico): {exc}")
