"""
/lead — comandos de Ricardo por WhatsApp. Solo un número con role=admin.
Escribir el comando ya es la confirmación; no hay diálogo extra.

Un lead nunca se vuelve usuario desde aquí: el alta es solo por código AMAEL-XXXXXXXX
(interfaces/api/routers/identity.py) o desde el panel.
"""
from __future__ import annotations

import logging
import re

from agents.reception import storage
from agents.reception.storage import Lead

logger = logging.getLogger("agents.reception.commands")

NOT_AVAILABLE = "Comando no disponible."
HELP = (
    "*Leads*\n"
    "/lead — pendientes\n"
    "/lead <n> — ficha e historial\n"
    "/lead <n> conversar — Amael le contesta como recepcionista (guarda todo en el lead)\n"
    "/lead <n> rechazar — silenciar 30 días\n"
    "/lead <n> responder <texto> — contestarle vía Amael\n"
    "↩️ O responde CITANDO el aviso del contacto."
)

_CMD_RE = re.compile(r"^(?P<id>\d+)(?:\s+(?P<action>conversar|rechazar|responder|ver))?(?:\s+(?P<text>.+))?$", re.S)


def parse(command: str) -> tuple[str, int | None, str]:
    """→ (action, lead_id, text). action ∈ list|help|show|conversar|rechazar|responder|invalid."""
    c = (command or "").strip()
    if not c or c.lower() in ("list", "lista", "pendientes"):
        return ("list", None, "")
    if c.lower() in ("ayuda", "help", "?"):
        return ("help", None, "")
    m = _CMD_RE.match(c)
    if not m or (m.group("text") and not m.group("action")):
        return ("invalid", None, "")
    lead_id = int(m.group("id"))
    action  = (m.group("action") or "show").lower()
    text    = (m.group("text") or "").strip()
    if action == "ver":
        action = "show"
    return (action, lead_id, text)


def is_admin_phone(phone: str) -> bool:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM user_identities ui
                JOIN user_profile up ON up.user_id = ui.canonical_user_id
                WHERE ui.identity_value = %s AND up.role = 'admin' AND up.status = 'active'
                UNION
                SELECT 1 FROM user_profile
                WHERE user_id = %s AND role = 'admin' AND status = 'active'
                LIMIT 1
                """,
                (phone, phone),
            )
            return cur.fetchone() is not None


def _ficha(lead: Lead) -> str:
    edad = ""
    if lead.created_at:
        from datetime import UTC, datetime
        h = int((datetime.now(UTC) - lead.created_at).total_seconds() // 3600)
        edad = f" · hace {h} h" if h < 48 else f" · hace {h // 24} d"
    return (
        f"*#{lead.id}* {lead.name or '(sin nombre)'} · {lead.company or '(sin empresa)'}\n"
        f"Motivo: {lead.reason or '—'}\nTel: {lead.phone} · {lead.status} · "
        f"{lead.message_count} msgs{edad}"
    )


def dispatch(command: str, phone: str) -> str:
    from observability.metrics import RECEPTION_LEADS_TOTAL

    if not is_admin_phone(phone):
        logger.warning(f"[reception] /lead desde no-admin {phone}")
        return NOT_AVAILABLE

    action, lead_id, text = parse(command)
    if action == "help":
        return HELP
    if action == "invalid":
        return f"No entendí el comando.\n\n{HELP}"
    if action == "list":
        leads = storage.list_open()
        if not leads:
            return "Sin leads pendientes."
        return "*Leads pendientes*\n\n" + "\n\n".join(_ficha(ld) for ld in leads)

    lead = storage.get(lead_id)
    if not lead:
        return f"No existe el lead #{lead_id}."

    if action == "show":
        hist = storage.recent_messages(lead.id, 10)
        icon = {"visitor": "👤", "amael": "🤖", "ricardo": "🧑‍💻"}
        lines = [f"{icon.get(r, '•')} {c[:300]}" for r, c in hist]
        return _ficha(lead) + "\n\n" + ("\n".join(lines) or "(sin mensajes)")

    if action == "conversar":
        from agents.reception import prompts
        from agents.reception.notify import send_to_visitor
        if lead.status != "private":
            return f"Lead #{lead.id} está en {lead.status}; conversar es solo para privados."
        storage.set_status(lead.id, "open")
        try:
            send_to_visitor(lead.phone, prompts.REPLY_CONVERSAR)
        except Exception as exc:
            logger.error(f"[reception] aviso de conversar falló: {exc}")
            return f"Lead #{lead.id} ya conversa con Amael, pero no le llegó el saludo."
        storage.add_message(lead.id, "amael", prompts.REPLY_CONVERSAR)
        return f"Lead #{lead.id}: Amael ya le contesta. Todo queda en lead_messages."

    if action == "rechazar":
        from agents.reception.receptionist import silence
        storage.set_status(lead.id, "rejected")
        try:
            silence(lead.phone)
        except Exception as exc:
            logger.warning(f"[reception] silence en Redis falló: {exc}")
        RECEPTION_LEADS_TOTAL.labels(event="rejected").inc()
        return f"Lead #{lead.id} rechazado; {lead.phone} en silencio 30 días."

    if action == "responder":
        if not text:
            return "Falta el texto: /lead <n> responder <mensaje>"
        from agents.reception.notify import send_to_visitor
        try:
            send_to_visitor(lead.phone, f"*Ricardo:* {text}")
        except Exception as exc:
            logger.error(f"[reception] responder falló: {exc}")
            return "No pude entregar el mensaje; el bridge no respondió."
        storage.add_message(lead.id, "ricardo", text)
        return f"Enviado a #{lead.id} ({lead.name or lead.phone})."

    return NOT_AVAILABLE
