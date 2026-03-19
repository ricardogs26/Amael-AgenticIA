"""
Router /api/devops — webhooks y operaciones DevOps (Camael).

Endpoints:
  POST /api/devops/ci-hook   — GitHub webhook para workflow_run events
                               Notifica por WhatsApp cuando un workflow falla
                             — pull_request events: notifica cuando se abre un PR hacia main
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, status

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
