"""
handle_message(): la conversación con un número desconocido.

Todo lo que decide vive en código: límites (Redis), silencio, cuándo avisar
a Ricardo, validación del JSON. El LLM solo redacta la respuesta y extrae
nombre/empresa/motivo.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime

from agents.reception import prompts, storage
from agents.reception.storage import Lead

logger = logging.getLogger("agents.reception")

MAX_TEXT_CHARS    = 500
MAX_REPLY_CHARS   = 1000
CONTEXT_TURNS     = 8
SUMMARY_EVERY     = 5      # tras el primer aviso, resumen cada N mensajes
FORCED_NOTIFY_AT  = 10     # lead incompleto → aviso igual a los N mensajes
SILENCE_TTL_S     = 30 * 24 * 3600
LLM_TIMEOUT_S     = 45

_K_RATE_PHONE = "reception:rate:{phone}"
_K_RATE_DAY   = "reception:rate:global:{day}"
_K_SILENCED   = "reception:silenced:{phone}"


# ── Funciones puras ───────────────────────────────────────────────────────────

def _fold(text: str) -> str:
    """minúsculas, sin acentos, sin puntuación, espacios colapsados."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    t = re.sub(r"[^a-z0-9. ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_trigger(text: str) -> bool:
    """
    El enlace del sitio manda «Hola Amael, vengo de richardx.dev». Solo esa
    frase (mayúsculas/acentos/puntuación aparte) abre una conversación con un
    número desconocido; cualquier otro mensaje se ignora en silencio. Sin esto
    el bot quedaba abierto a cualquiera que tuviera el número.
    """
    f = _fold(text)
    return "hola amael" in f and "richardx.dev" in f


def parse_llm_json(raw: str) -> dict:
    """
    Devuelve siempre un dict con reply/name/company/reason. JSON inválido,
    no-objeto o reply vacío/largo → reply de respaldo y campos vacíos.
    """
    empty = {"reply": prompts.REPLY_FALLBACK, "name": None, "company": None, "reason": None}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip() or len(reply) > MAX_REPLY_CHARS:
        reply = prompts.REPLY_FALLBACK
    out = {"reply": reply.strip()}
    for f in storage.FIELDS:
        v = data.get(f)
        out[f] = v.strip() if isinstance(v, str) and v.strip() else None
    return out


def should_notify(lead: Lead, completed_now: bool, count: int) -> str | None:
    """
    "first"   — se completaron los 3 datos y nunca se avisó.
    "forced"  — nunca se avisó, sigue incompleto, pero ya van FORCED_NOTIFY_AT.
    "summary" — ya se avisó; cada SUMMARY_EVERY mensajes un resumen.
    """
    if lead.notified_at is None:
        if completed_now:
            return "first"
        if count >= FORCED_NOTIFY_AT:
            return "forced"
        return None
    if count % SUMMARY_EVERY == 0:
        return "summary"
    return None


# ── Redis ─────────────────────────────────────────────────────────────────────

def _redis():
    from storage.redis.client import get_redis_client
    return get_redis_client()


def _limits() -> tuple[int, int]:
    from config.settings import settings
    return (int(getattr(settings, "reception_max_per_phone", 15)),
            int(getattr(settings, "reception_max_per_day", 100)))


def check_limits(phone: str) -> str | None:
    """None = puede pasar; si no, la razón ('phone' | 'global')."""
    per_phone, per_day = _limits()
    r = _redis()
    k = _K_RATE_PHONE.format(phone=phone)
    n = r.incr(k)
    if n == 1:
        r.expire(k, 24 * 3600)
    if n > per_phone:
        return "phone"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    kd = _K_RATE_DAY.format(day=day)
    g = r.incr(kd)
    if g == 1:
        r.expire(kd, 2 * 24 * 3600)
    if g > per_day:
        return "global"
    return None


def is_silenced(phone: str) -> bool:
    try:
        return bool(_redis().exists(_K_SILENCED.format(phone=phone)))
    except Exception as exc:  # Redis caído: mejor contestar que callar
        logger.warning(f"[reception] silenced check falló: {exc}")
        return False


def silence(phone: str) -> None:
    _redis().setex(_K_SILENCED.format(phone=phone), SILENCE_TTL_S, "1")


# ── LLM ───────────────────────────────────────────────────────────────────────

def _ask_llm(lead: Lead, history: list[tuple[str, str]], text: str) -> str:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    from config.settings import settings

    llm = ChatOllama(
        model=settings.llm_model_fast,
        base_url=settings.ollama_base_url,
        temperature=0.3,
        reasoning=False,
        format="json",
        client_kwargs={"timeout": LLM_TIMEOUT_S},
    )
    captured = {f: getattr(lead, f) for f in storage.FIELDS}
    msgs = [SystemMessage(content=prompts.build_system(captured))]
    for role, content in history:
        if role == "visitor":
            msgs.append(HumanMessage(content=content))
        elif role == "amael":
            msgs.append(AIMessage(content=json.dumps({"reply": content}, ensure_ascii=False)))
        else:  # ricardo
            msgs.append(AIMessage(content=json.dumps({"reply": f"Ricardo: {content}"}, ensure_ascii=False)))
    msgs.append(HumanMessage(content=text))
    resp = llm.invoke(msgs)
    return resp.content if isinstance(resp.content, str) else str(resp.content)


# ── Aviso a Ricardo ───────────────────────────────────────────────────────────

def _notify_admin(kind: str, lead: Lead, history: list[tuple[str, str]]) -> None:
    from agents.reception.notify import send_to_admin

    if kind in ("first", "forced"):
        faltan = [f for f in storage.FIELDS if not getattr(lead, f)]
        head = "📩 *Nuevo contacto*" if kind == "first" else "📩 *Contacto sin completar*"
        txt = (
            f"{head} #{lead.id}: {lead.name or '(sin nombre)'} · "
            f"{lead.company or '(sin empresa)'} · «{lead.reason or 'sin motivo'}»\n"
            f"Tel: {lead.phone}"
        )
        if faltan:
            txt += f"\nFalta: {', '.join(faltan)}"
        txt += f"\n\n/lead {lead.id} para verlo · /lead {lead.id} responder <texto>"
    else:
        ult = [c for r, c in history if r == "visitor"][-SUMMARY_EVERY:]
        txt = (
            f"💬 *Lead #{lead.id}* ({lead.name or lead.phone}) sigue escribiendo "
            f"({lead.message_count} msgs):\n" +
            "\n".join(f"• {c[:120]}" for c in ult)
        )
    try:
        send_to_admin(txt)
        storage.mark_notified(lead.id)
    except Exception as exc:
        logger.error(f"[reception] aviso a admin falló: {exc}")


# ── Entrada principal ─────────────────────────────────────────────────────────

def handle_message(phone: str, text: str, has_media: bool = False) -> str | None:
    from observability.metrics import RECEPTION_LEADS_TOTAL, RECEPTION_MESSAGES_TOTAL

    phone = (phone or "").strip()
    text  = (text or "").strip()[:MAX_TEXT_CHARS]

    if is_silenced(phone):
        RECEPTION_MESSAGES_TOTAL.labels(result="silenced").inc()
        return None

    lead = storage.get_by_phone(phone)
    if lead is None:
        if not is_trigger(text):
            RECEPTION_MESSAGES_TOTAL.labels(result="ignored").inc()
            logger.info(f"[reception] {phone} sin frase de activación — ignorado")
            return None
        lead = storage.get_or_create(phone)
        RECEPTION_LEADS_TOTAL.labels(event="created").inc()
    if lead.status == "rejected":
        RECEPTION_MESSAGES_TOTAL.labels(result="silenced").inc()
        return None

    if has_media and not text:
        RECEPTION_MESSAGES_TOTAL.labels(result="media").inc()
        return prompts.REPLY_MEDIA
    if not text:
        return None

    try:
        limited = check_limits(phone)
    except Exception as exc:
        logger.warning(f"[reception] Redis no disponible para límites: {exc}")
        limited = None
    if limited:
        RECEPTION_MESSAGES_TOTAL.labels(result="rate_limited").inc()
        storage.add_message(lead.id, "visitor", text)
        storage.bump_count(lead.id)
        return prompts.REPLY_LIMIT

    history = storage.recent_messages(lead.id, CONTEXT_TURNS)
    was_complete = lead.complete
    try:
        raw = _ask_llm(lead, history, text)
        parsed = parse_llm_json(raw)
    except Exception as exc:
        logger.error(f"[reception] LLM falló para lead #{lead.id}: {exc}")
        RECEPTION_MESSAGES_TOTAL.labels(result="error").inc()
        storage.add_message(lead.id, "visitor", text)
        storage.bump_count(lead.id)
        return prompts.REPLY_ERROR

    nuevos = storage.merge_fields(lead, parsed)
    if nuevos:
        storage.update_fields(lead.id, **nuevos)
        for k, v in nuevos.items():
            setattr(lead, k, v)

    storage.add_message(lead.id, "visitor", text)
    storage.add_message(lead.id, "amael", parsed["reply"])
    count = storage.bump_count(lead.id)
    lead.message_count = count
    RECEPTION_MESSAGES_TOTAL.labels(result="replied").inc()

    completed_now = lead.complete and not was_complete
    if completed_now:
        RECEPTION_LEADS_TOTAL.labels(event="completed").inc()
    kind = should_notify(lead, completed_now, count)
    if kind:
        _notify_admin(kind, lead, history + [("visitor", text)])

    return parsed["reply"]
