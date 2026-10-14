"""
Análisis nocturno de pendientes + sección «Pendientes» del brief (spec
Cassiel Fase 2 §4, implementado el 14-oct-2026).

03:45 México, en el scheduler del backend, con lock Redis (2 réplicas):
  1. Compromisos desde correo (agents/productivity/commitment_extractor):
     pagos, vencimientos, citas que llegaron por Gmail → pendientes de
     Cassiel con `source='email'`.
  2. Análisis de los pendientes de cada usuario: el LLM señala por dónde
     empezar, cuáles están estancadas y UNA pregunta; el código valida los
     ids, calcula las estancadas por su cuenta y redacta. Se guarda en
     `task_briefs` para que el brief de las 7:00 lo anexe.

Garantía: el brief JAMÁS se bloquea por esto. Si el nocturno falló, el
brief sale con la lista ordenada por código, sin análisis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("agents.scheduler.night_analysis")

_TZ = "America/Mexico_City"
_LLM_TIMEOUT_S = int(os.environ.get("NIGHT_ANALYSIS_LLM_TIMEOUT_S", "180"))
STALLED_DAYS   = int(os.environ.get("TASKS_STALLED_DAYS", "7"))
TOP_N          = 3

_DDL = """
CREATE TABLE IF NOT EXISTS task_briefs (
    id         SERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    brief_date DATE NOT NULL,
    analysis   TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, brief_date)
);
"""

_SYSTEM = """Eres Cassiel, el asistente de pendientes de Amael. Recibes la lista de pendientes de un usuario (id, título, prioridad, fecha límite, días desde que se anotó) y la fecha de hoy. Devuelve SOLO un objeto JSON:

{"top": [ids por los que conviene empezar hoy, máximo 3, en orden],
 "stalled": [ids que llevan demasiado sin moverse],
 "question": "UNA pregunta corta si falta un dato para avanzar alguna, o null"}

Reglas: usa SOLO ids de la lista; no inventes tareas; la pregunta es opcional y es una sola. Responde SOLO el JSON."""


def init_briefs_db() -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    logger.info("[night] Tabla task_briefs lista.")


# ── Código: pendientes → texto ───────────────────────────────────────────────

def _dias(task, today: date) -> str:
    if task.due_date is None:
        return ""
    d = (task.due_date - today).days
    if d < 0:
        n = -d
        return f"{n} día{'s' if n != 1 else ''} de atraso"
    if d == 0:
        return "para hoy"
    if d == 1:
        return "vence mañana"
    return f"vence en {d} días"


def _monto(task) -> str:
    if task.amount is None:
        return ""
    return f" · ${task.amount:,.0f}" if float(task.amount).is_integer() \
        else f" · ${task.amount:,.2f}"


def _linea(task, today: date) -> str:
    marca = ("🔴" if task.due_date and task.due_date < today else
             "🟡" if task.due_date == today else "•")
    cuando = _dias(task, today)
    origen = " (del correo)" if task.source == "email" else ""
    partes = [f"{marca} #{task.id} {task.title}"]
    if cuando:
        partes.append(f" — {cuando}")
    partes.append(_monto(task))
    partes.append(origen)
    return "".join(partes)


def build_pending_section(tasks: list, today: date, analysis: str = "") -> str:
    """Sección «Pendientes» del brief. Orden determinista (sort_key de
    Cassiel): vencidas > hoy > alta > media > baja. Sin tareas → ''."""
    from agents.scheduler.tasks_storage import sorted_pending

    pendientes = [t for t in tasks if t.status == "pending"]
    if not pendientes:
        return ""
    orden = sorted_pending(pendientes, today)
    lineas = ["📝 *Pendientes*"]
    lineas += [_linea(t, today) for t in orden[:TOP_N]]
    resto = len(orden) - TOP_N
    if resto > 0:
        lineas.append(f"(y {resto} más — «/pendientes» para verlas)")
    if analysis and analysis.strip():
        lineas.append(analysis.strip())
    return "\n".join(lineas)


def _stalled_by_code(tasks: list, today: date) -> list[int]:
    out = []
    for t in tasks:
        if t.due_date is not None:
            continue
        creada = t.created_at.date() if isinstance(t.created_at, datetime) else t.created_at
        if creada and (today - creada).days > STALLED_DAYS:
            out.append(t.id)
    return out


def render_analysis(parsed, tasks: list, today: date) -> str:
    """El LLM propone ids y una pregunta; aquí se valida contra la lista real
    y se redacta. Ids inventados se ignoran; las estancadas se completan con
    la regla de código (sin fecha y > STALLED_DAYS días anotada)."""
    if not isinstance(parsed, dict):
        parsed = {}
    por_id = {t.id: t for t in tasks if t.status == "pending"}

    def _ids(clave) -> list[int]:
        vals = parsed.get(clave)
        if not isinstance(vals, list):
            return []
        out = []
        for v in vals:
            try:
                i = int(str(v).strip().lstrip("#").strip())   # el 9b escribe «#4»
            except (TypeError, ValueError):
                continue
            if i in por_id and i not in out:
                out.append(i)
        return out

    top = _ids("top")[:TOP_N]
    stalled = _ids("stalled")
    for i in _stalled_by_code(list(por_id.values()), today):
        if i not in stalled:
            stalled.append(i)
    q = parsed.get("question")
    if isinstance(q, list):
        q = q[0] if q else None
    pregunta = str(q).strip() if q else ""

    lineas = []
    if top:
        lineas.append("💡 Empieza por: " + "; ".join(f"#{i} {por_id[i].title}" for i in top))
    if stalled:
        lineas.append("⏳ Estancadas: " + "; ".join(f"#{i} {por_id[i].title}" for i in stalled))
    if pregunta:
        lineas.append(f"❓ {pregunta[:200]}")
    return "\n".join(lineas)


# ── LLM ──────────────────────────────────────────────────────────────────────

def _format_tasks_for_llm(tasks: list, today: date) -> str:
    lineas = [f"Hoy es {today.isoformat()}.", "", "Pendientes:"]
    for t in tasks:
        creada = t.created_at.date() if isinstance(t.created_at, datetime) else t.created_at
        edad = (today - creada).days if creada else "?"
        lineas.append(f"- #{t.id} {t.title} | prioridad {t.priority} | "
                      f"límite {t.due_date or '—'} | anotada hace {edad} días")
    return "\n".join(lineas)


def analyze_tasks_with_llm(tasks: list, today: date) -> dict:
    """Bloqueante. Una llamada, JSON forzado, sin razonamiento."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from agents.base.llm_factory import get_chat_llm

    llm = get_chat_llm(temperature=0.1, timeout=_LLM_TIMEOUT_S, reasoning=False, fmt="json")
    resp = llm.invoke([SystemMessage(content=_SYSTEM),
                       HumanMessage(content=_format_tasks_for_llm(tasks, today))])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


# ── Persistencia ─────────────────────────────────────────────────────────────

def save_brief(user_id: str, brief_date: date, analysis: str) -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_briefs (user_id, brief_date, analysis) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, brief_date) DO UPDATE SET analysis = EXCLUDED.analysis, "
                "created_at = NOW()",
                (user_id, brief_date, analysis),
            )
        conn.commit()


def get_brief_analysis(user_id: str, brief_date: date) -> str:
    """Análisis guardado para esa fecha, o '' si el nocturno no corrió."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT analysis FROM task_briefs WHERE user_id = %s AND brief_date = %s",
                    (user_id, brief_date),
                )
                row = cur.fetchone()
        return (row[0] or "") if row else ""
    except Exception as exc:
        logger.warning(f"[night] No se pudo leer task_briefs de {user_id}: {exc}")
        return ""


def pending_section_for(user_id: str, today: date) -> str:
    """Sección lista para pegar en el brief. Best-effort: nunca lanza."""
    try:
        from agents.scheduler.tasks_storage import list_pending
        tareas = list_pending(user_id)
        return build_pending_section(tareas, today, get_brief_analysis(user_id, today))
    except Exception as exc:
        logger.warning(f"[night] Sección de pendientes de {user_id} falló: {exc}")
        return ""


# ── Corrida nocturna ─────────────────────────────────────────────────────────

def _brief_users() -> list[str]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM user_profile WHERE status = 'active' "
                "AND daily_brief_enabled = TRUE AND user_id LIKE '%%@%%' ORDER BY user_id"
            )
            return [r[0] for r in cur.fetchall()]


def run_night_analysis_sync(now: datetime | None = None) -> list[dict]:
    """Para cada usuario con brief: compromisos del correo + análisis de
    pendientes. Cada paso es best-effort e independiente."""
    from agents.productivity import commitment_extractor
    from agents.scheduler import tasks_storage

    now = now or datetime.now(ZoneInfo(_TZ))
    today = now.date()
    resultados = []
    for user in _brief_users():
        res = {"user": user}
        try:
            res["commitments"] = commitment_extractor.run_for_user(user, now)
        except Exception as exc:
            logger.warning(f"[night] Compromisos de {user} fallaron: {exc}")
            res["commitments"] = {"status": f"error: {exc}"}
        try:
            tareas = tasks_storage.list_pending(user)
            analysis = ""
            if tareas:
                analysis = render_analysis(analyze_tasks_with_llm(tareas, today), tareas, today)
            save_brief(user, today, analysis)
            res["analysis_chars"] = len(analysis)
        except Exception as exc:
            logger.warning(f"[night] Análisis de {user} falló: {exc}")
            res["analysis"] = f"error: {exc}"
        resultados.append(res)
    logger.info(f"[night] Nocturno terminado para {len(resultados)} usuario(s).")
    return resultados


async def run_night_analysis() -> list[dict]:
    return await asyncio.to_thread(run_night_analysis_sync)
