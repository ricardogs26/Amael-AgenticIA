"""
Compromisos desde el correo: pagos, vencimientos, citas y respuestas
pendientes que llegan por Gmail y siguen abiertos hasta que el usuario los
cierra — aunque ya haya abierto el correo.

Por qué existe (14-oct-2026): el brief de las 7:00 solo leía correos NO
LEÍDOS y excluía la categoría `updates`, donde Gmail mete los recibos. El
pago de la línea telefónica nunca aparecía: o estaba en `updates`, o Ricardo
ya lo había abierto. «No leído» es una señal de atención, no de estado.

Reparto — el mismo de Cassiel y del extractor de citas:
  - el LLM SOLO extrae `{email_index, type, title, due_date, amount}` de un
    lote de correos (una llamada, JSON forzado, sin razonamiento);
  - el CÓDIGO valida fecha (hoy ≤ due ≤ hoy+60 días), tipo, índice; decide
    prioridad por cercanía; deduplica por id de Gmail; respeta remitentes
    excluidos; y registra cada compromiso como pendiente de Cassiel con
    `source='email'`, para que entre al circuito normal (brief, nudges,
    «ya lo pagué», «no me interesa»).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger("agents.productivity.commitment_extractor")

_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL           = os.environ.get("LLM_MODEL", "qwen3.5:9b")
_LLM_TIMEOUT_S   = int(os.environ.get("COMMITMENT_LLM_TIMEOUT_S", "120"))
MAX_DAYS_AHEAD   = int(os.environ.get("COMMITMENT_MAX_DAYS_AHEAD", "60"))
EMAIL_LOOKBACK_DAYS = int(os.environ.get("COMMITMENT_EMAIL_DAYS", "7"))
EMAIL_MAX_RESULTS   = int(os.environ.get("COMMITMENT_EMAIL_MAX", "40"))

TYPES = ("pago", "vencimiento", "cita", "respuesta")

_SYSTEM = """Eres Cassiel, el asistente de pendientes de Amael. Recibes una lista numerada de correos (remitente, asunto, extracto) y la fecha de hoy. Devuelve SOLO un objeto JSON:

{"commitments": [
  {"email_index": <índice del correo>, "type": "pago" | "vencimiento" | "cita" | "respuesta",
   "title": "qué hay que hacer, corto (ej. «Pagar factura Telcel octubre»)",
   "due_date": "YYYY-MM-DD" | null, "amount": número | null}
]}

Un compromiso es algo que el usuario TIENE QUE HACER antes de una fecha: pagar un recibo, renovar algo que vence, asistir a una cita, contestar algo que le pidieron. NO son compromisos: boletines, promociones, confirmaciones de pagos YA hechos, notificaciones informativas, recibos domiciliados que dicen «se cargará automáticamente».
Reglas:
- Copia la fecha límite del correo. Si no da fecha, pon null (el código lo descarta).
- «20 de octubre» sin año → usa el año que corresponda a partir de hoy.
- Un correo puede dar cero compromisos. Si ninguno, devuelve {"commitments": []}.
- Responde SOLO el JSON."""


@dataclass
class Commitment:
    title: str
    type: str
    due_date: date
    priority: str
    sender: str
    source_ref: str
    description: str
    amount: float | None = None


# ── Correos → texto para el LLM ─────────────────────────────────────────────

def format_emails_for_llm(emails: list[dict], today: date) -> str:
    lineas = [f"Hoy es {today.isoformat()}.", "", "Correos:"]
    for i, em in enumerate(emails):
        lineas.append(
            f"[{i}] De: {em.get('from', '')} | Asunto: {em.get('subject', '')} | "
            f"Extracto: {str(em.get('snippet', ''))[:400]}"
        )
    return "\n".join(lineas)


def extract_commitment_fields(emails: list[dict], today: date) -> dict:
    """Llama al LLM en modo JSON. Bloqueante — el llamador lo manda a un hilo."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    llm = ChatOllama(
        model=_MODEL, base_url=_OLLAMA_BASE_URL, temperature=0.0,
        reasoning=False, format="json", client_kwargs={"timeout": _LLM_TIMEOUT_S},
    )
    resp = llm.invoke([SystemMessage(content=_SYSTEM),
                       HumanMessage(content=format_emails_for_llm(emails, today))])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


# ── Código: JSON → compromisos validados ─────────────────────────────────────

_EMAIL_RE = re.compile(r"<([^>]+)>")

# Cargos automáticos: el prompt dice que no son compromisos y el 9b igual
# marcó «Pagar recibo CFE (domiciliado)» (sondeo 14-oct-2026). Regla en código:
# un PAGO cuyo correo anuncia cargo automático se descarta. Solo aplica a
# `pago`/`vencimiento` — una cita del mismo remitente sí pasa.
_AUTO_CARGO_RE = re.compile(
    r"se\s+cargar[áa]\s+autom|cargo\s+autom[áa]tico|domicilia|"
    r"no\s+necesitas\s+hacer\s+nada|pago\s+autom[áa]tico",
    re.IGNORECASE,
)


def _sender_address(from_header: str) -> str:
    m = _EMAIL_RE.search(from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def _priority_for(due: date, today: date) -> str:
    dias = (due - today).days
    if dias <= 3:
        return "alta"
    if dias <= 7:
        return "media"
    return "baja"


def _parse_amount(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def resolve_commitments(parsed: dict, emails: list[dict], today: date) -> list[Commitment]:
    items = parsed.get("commitments") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    out: list[Commitment] = []
    limite = today + timedelta(days=MAX_DAYS_AHEAD)
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("email_index"))
            em = emails[idx]
        except (TypeError, ValueError, IndexError):
            continue
        tipo = str(it.get("type") or "").strip().lower()
        if tipo not in TYPES:
            continue
        title = str(it.get("title") or "").strip()[:120]
        if not title:
            continue
        try:
            due = date.fromisoformat(str(it.get("due_date") or "")[:10])
        except ValueError:
            continue
        if due < today or due > limite:
            continue
        if tipo in ("pago", "vencimiento") and _AUTO_CARGO_RE.search(
            f"{em.get('subject', '')} {em.get('snippet', '')}"
        ):
            logger.info(f"[commitments] cargo automático, no es compromiso: {title!r}")
            continue
        sender = _sender_address(em.get("from", ""))
        out.append(Commitment(
            title=title, type=tipo, due_date=due,
            priority=_priority_for(due, today),
            sender=sender, source_ref=f"gmail:{em.get('id', '')}",
            description=(f"Del correo de {em.get('from', '')}: "
                         f"«{em.get('subject', '')}»")[:1000],
            amount=_parse_amount(it.get("amount")),
        ))
    return out


def register_commitments(
    user_id: str,
    commitments: list[Commitment],
    *,
    ya_registrado: Callable[[str], bool],
    excluido: Callable[[str], bool],
    crear: Callable[..., object],
) -> int:
    """Registra como pendientes los compromisos nuevos. Las dependencias
    entran como callables para que la regla sea pura y probable sin DB."""
    n = 0
    for c in commitments:
        if ya_registrado(c.source_ref):
            continue
        if excluido(c.sender):
            logger.info(f"[commitments] {c.sender} excluido por {user_id}; omitido {c.title!r}")
            continue
        crear(
            user_id=user_id, title=c.title, description=c.description,
            category="personal", priority=c.priority, due_date=c.due_date,
            source="email", source_ref=c.source_ref, source_sender=c.sender,
            amount=c.amount,
        )
        n += 1
    return n


# ── Orquestación por usuario ─────────────────────────────────────────────────

def run_for_user(user_email: str, now: datetime) -> dict:
    """Barrido completo de un usuario. Bloqueante (Gmail + LLM + Postgres)."""
    from agents.productivity.email_manager import get_recent_emails
    from agents.productivity.vault_credentials import get_user_credentials
    from agents.scheduler import tasks_storage

    creds = get_user_credentials(user_email)
    if not creds:
        return {"user": user_email, "status": "sin_credenciales", "registrados": 0}
    emails = get_recent_emails(creds, days=EMAIL_LOOKBACK_DAYS, max_results=EMAIL_MAX_RESULTS)
    if not emails:
        return {"user": user_email, "status": "ok", "correos": 0, "registrados": 0}
    today = now.date()
    parsed = extract_commitment_fields(emails, today)
    resueltos = resolve_commitments(parsed, emails, today)
    n = register_commitments(
        user_email, resueltos,
        ya_registrado=lambda ref: tasks_storage.source_ref_exists(user_email, ref),
        excluido=lambda sender: tasks_storage.sender_excluded(user_email, sender),
        crear=tasks_storage.create_task,
    )
    logger.info(f"[commitments] {user_email}: {len(emails)} correos, "
                f"{len(resueltos)} compromisos, {n} nuevos.")
    return {"user": user_email, "status": "ok", "correos": len(emails),
            "compromisos": len(resueltos), "registrados": n}
