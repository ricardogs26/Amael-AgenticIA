"""
Day Planner — genera y sincroniza el plan del día usando LLM + Google APIs.

Migrado desde productivity-service/app/services/planner_service.py:
  organize_day_for_user() — pipeline completo: credenciales → datos → LLM → calendario

Invocado por el CronJob `day-planner` cada weekday a las 7:00am (Mexico City).
También disponible para el agente conversacional vía PRODUCTIVITY_TOOL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger("agents.productivity.planner")

# Zona horaria por defecto cuando el perfil del usuario no dice otra cosa. El
# planner corría con la hora del POD (UTC): a las 7:00 de México ya era el día
# siguiente en UTC durante parte del año, así que el plan podía fecharse mal.
_TZ_DEFAULT = "America/Mexico_City"

# Singleton LLM
_planner_llm = None
# Espera del plan: el cliente tiene 180 s; aquí 150 s (la lección de raphael
# 1.1.31: cliente y espera nunca se escriben por separado sin relación).
_LLM_WAIT_S = int(os.environ.get("PLANNER_LLM_WAIT_S", "150"))


def _parse_plan(raw: Any) -> dict:
    """
    Convierte la respuesta del LLM en el dict del plan.

    `_get_llm()` devuelve un ChatOllama, y `.invoke()` de un modelo de chat
    responde un `AIMessage`, no una cadena. Pasárselo directo a `json.loads`
    reventaba con «the JSON object must be str, bytes or bytearray, not
    AIMessage» y el brief salía como "❌ No se pudo generar un plan válido"
    — el plan nunca se generó por esta vía.

    Se acepta también una cadena por si el factory vuelve a un LLM de
    completado, y se extrae el objeto JSON aunque el modelo lo envuelva en
    prosa o en un bloque ```json.
    """
    text = getattr(raw, "content", raw)
    if isinstance(text, list):  # algunos modelos responden en bloques
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text)
    if not isinstance(text, str):
        text = str(text)
    # Una respuesta vacía es un fallo DISTINTO de una respuesta sin JSON, y hay
    # que decirlo: es la firma de qwen3 razonando (todo va a `thinking` y
    # `content` queda en blanco). Confundirla con un timeout llevó a subir el
    # reloj cuando lo que había que apagar era el thinking.
    if not text.strip():
        raise ValueError(
            "el LLM devolvió una respuesta vacía (¿thinking activado?, "
            "el contenido se va al campo 'thinking' y 'content' queda vacío)"
        )
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"sin JSON en la respuesta del LLM: {text[:200]}")
    return json.loads(m.group(0))


def _get_llm():
    """
    LLM del plan diario: SIN thinking y con timeout holgado.

    Medido el 14-ago-2026 con el prompt real: razonando, qwen3.5:9b tarda ~83 s
    y devuelve `content` VACÍO — agota la generación pensando y nunca emite la
    respuesta. El brief moría con «sin JSON» o, si el reloj llegaba antes, con
    «el LLM tardó demasiado»; subir el timeout solo habría cambiado el mensaje.

    El nombre del parámetro importa y costó una iteración: ChatOllama lo llama
    `reasoning`. `think` es el nombre en la API de Ollama y langchain lo
    DESCARTA sin avisar — con `think=False` el modelo seguía razonando y la
    única razón de que una prueba saliera bien fue el azar de la temperatura.

    `fmt="json"` es el segundo cinturón: el plan DEBE ser JSON. Es el mismo
    patrón que ya usa Cassiel para traducir intención a JSON.

    El timeout de 60 s original tampoco daba margen; 180 s es holgado para un
    cron de las 7 am donde nadie espera frente a la pantalla.
    """
    global _planner_llm
    if _planner_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _planner_llm = get_chat_llm(timeout=180, reasoning=False, fmt="json")
    return _planner_llm


_PLANNING_PROMPT = """
Eres un asistente de productividad experto. Analiza los eventos del calendario y los emails
no leídos del usuario y genera un plan de día optimizado.

Fecha: {date}
{profile}
EVENTOS DE HOY:
{events}

EMAILS NO LEÍDOS:
{emails}
{pendientes}
Genera un plan de día en formato JSON con esta estructura exacta:
{{
  "summary": "Resumen breve del día en 2-3 oraciones",
  "tasks": [
    {{"title": "Nombre de la tarea", "start": "HH:MM", "end": "HH:MM", "description": "Contexto"}},
    ...
  ],
  "priorities": ["Prioridad 1", "Prioridad 2", "Prioridad 3"],
  "warnings": ["Conflicto o problema detectado"]
}}

Reglas:
- Horario laboral: 08:00 - 18:00
- Bloques de concentración: mínimo 90 minutos
- Incluir descansos de 15 min cada 2 horas
- Priorizar emails urgentes que requieran respuesta hoy
- Cada bloque de concentración debe decir en su título A QUÉ objetivo o proyecto
  del usuario sirve. Un título genérico como "Bloque de concentración" no ayuda
  a nadie: usa el nombre real del trabajo que toca.
- NO inventes objetivos que no estén en el contexto del usuario. Si no hay
  ninguno listado, deja los bloques genéricos.
- Los eventos ya agendados NO se repiten en el plan: ya existen en el
  calendario. Planifica ALREDEDOR de ellos.
- Si hay PENDIENTES listados, los bloques deben servir para avanzarlos (usa
  su título); los vencidos o de hoy van primero.
- Responde ÚNICAMENTE con el JSON, sin explicaciones adicionales.
""".strip()


def _build_planning_prompt(date: str, events: list, emails: list, perfil: str = "",
                           pendientes: str = "") -> str:
    """
    Arma el prompt del plan. `perfil` son los hechos que la memoria destiló del
    usuario (proyectos en curso, preferencias); sin ellos el LLM solo puede
    producir "Bloque de concentración" en abstracto, que es lo que llenaba el
    calendario de bloques idénticos e inútiles.

    Cuando no hay hechos, la sección se OMITE entera: un encabezado
    "OBJETIVOS:" en blanco es una invitación a inventarlos.
    """
    bloque = (
        f"\nOBJETIVOS Y CONTEXTO DEL USUARIO:\n{perfil.strip()}\n"
        if perfil and perfil.strip()
        else ""
    )
    bloque_pend = f"\nPENDIENTES DEL USUARIO:\n{pendientes.strip()}\n" if pendientes.strip() else ""
    return _PLANNING_PROMPT.format(
        date=date,
        profile=bloque,
        events=_format_events(events),
        emails=_format_emails(emails),
        pendientes=bloque_pend,
    )


def _render_profile_block(user_email: str) -> str:
    """Indirección para poder sustituirla en las pruebas."""
    from agents.memory_agent.profile import render_profile_block
    return render_profile_block(user_email)


def _zona_horaria_de(user_email: str) -> str:
    """Zona horaria del perfil del usuario; el default si no hay o falla."""
    try:
        from storage.postgres.client import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT timezone FROM user_profile WHERE user_id = %s", (user_email,))
            fila = cur.fetchone()
        if fila and fila[0]:
            return fila[0]
    except Exception as exc:
        logger.warning(f"[planner] No se pudo leer la zona horaria de {user_email}: {exc}")
    return _TZ_DEFAULT


def _hoy_para(user_email: str) -> datetime:
    """
    «Hoy» en la zona del usuario, no en la del pod. El plan se escribe con la
    fecha que el usuario ve en su calendario.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(_zona_horaria_de(user_email)))
    except Exception as exc:
        logger.warning(f"[planner] Zona horaria inválida para {user_email}: {exc}")
        return datetime.now()


def _pendientes_de(user_email: str) -> list:
    """Pendientes de Cassiel. Best-effort — nunca bloquea el brief."""
    try:
        from agents.scheduler.tasks_storage import list_pending
        return list_pending(user_email)
    except Exception as exc:
        logger.warning(f"[planner] No se pudieron leer los pendientes de {user_email}: {exc}")
        return []


def _analisis_de(user_email: str, hoy: date) -> str:
    """Análisis del nocturno (task_briefs) para hoy, o '' si no corrió."""
    from agents.scheduler.night_analysis import get_brief_analysis
    return get_brief_analysis(user_email, hoy)


def _format_pendientes_prompt(tareas: list, hoy: date) -> str:
    from agents.scheduler.tasks_storage import sorted_pending
    lineas = []
    for t in sorted_pending(tareas, hoy)[:8]:
        fecha = f" (límite {t.due_date})" if t.due_date else ""
        lineas.append(f"- #{t.id} {t.title} [{t.priority}]{fecha}")
    return "\n".join(lineas)


def _perfil_de(user_email: str) -> str:
    """
    Hechos del usuario, best-effort. Esto corre en un cron a las 7:00 am: si la
    memoria falla, el brief tiene que salir de todos modos — degradado, no
    ausente.
    """
    try:
        return _render_profile_block(user_email) or ""
    except Exception as exc:
        logger.warning(f"[planner] No se pudo leer el perfil de {user_email}: {exc}")
        return ""


def _format_events(events: list) -> str:
    if not events:
        return "(sin eventos)"
    lines = []
    for e in events:
        lines.append(f"- {e.get('start', '')} - {e.get('end', '')}: {e.get('summary', '')}")
        if e.get("description"):
            lines.append(f"  Descripción: {e['description'][:100]}")
    return "\n".join(lines)


def _format_emails(emails: list) -> str:
    if not emails:
        return "(sin emails no leídos)"
    lines = []
    for em in emails[:10]:  # top 10 para no saturar el contexto
        lines.append(
            f"- De: {em.get('from', '')} | Asunto: {em.get('subject', '')} "
            f"| Snippet: {em.get('snippet', '')[:100]}"
        )
    return "\n".join(lines)


async def organize_day_for_user(user_email: str) -> dict[str, Any]:
    """
    Brief del día de un usuario (Fase 2 C/D, 14-oct-2026) — el CÓDIGO reúne y
    ordena; el LLM solo reparte bloques:
      0. Pendientes de Cassiel + análisis nocturno (no dependen de Google)
      1. Credenciales Google desde Vault
      2. Eventos de hoy, próximos 7 días y correos no leídos
      3. Plan de bloques con el LLM → validado en código (horario, choques)
      4. Bloques válidos al calendario (reemplazando el plan anterior)
      5. Brief ensamblado por secciones fijas; si el LLM falla, sale sin 🧭
    """
    from agents.productivity import brief_builder as bb
    from agents.productivity.calendar_manager import (
        get_todays_events,
        get_upcoming_events,
        sync_plan_to_calendar,
    )
    from agents.productivity.email_manager import get_unread_emails
    from agents.productivity.errors import GoogleAuthRevocada
    from agents.productivity.vault_credentials import get_user_credentials
    from agents.scheduler.night_analysis import build_pending_section

    hoy = _hoy_para(user_email)
    fecha = hoy.date()
    alertas: list[str] = []
    error: str | None = None

    # 0. Pendientes
    tareas = await asyncio.to_thread(_pendientes_de, user_email)
    try:
        analisis = await asyncio.to_thread(_analisis_de, user_email, fecha)
    except Exception as exc:
        logger.warning(f"[planner] Sin análisis nocturno para {user_email}: {exc}")
        analisis = ""
    pending_txt = build_pending_section(tareas, fecha, analisis)
    due_txt = bb.section_due_week(tareas, fecha)

    # 1-2. Google
    events: list = []
    emails: list = []
    proximos: list = []
    credentials = get_user_credentials(user_email)
    if not credentials:
        error = "no_credentials"
        alertas.append("🔑 No hay credenciales de Google: no pude leer tu calendario ni tu "
                       "correo. Autorízalas en https://amael-ia.richardx.dev (botón «Calendar»).")
    else:
        # Si la autorización está revocada hay que DECIRLO: el lector devolvía
        # [] y el brief concluía «tu día está libre» con el calendario lleno
        # (14-ago-2026). Y no se escribe nada en un calendario que no se pudo leer.
        try:
            events = get_todays_events(credentials)
            emails = get_unread_emails(credentials)
            proximos = get_upcoming_events(credentials, now=hoy)
        except GoogleAuthRevocada as exc:
            logger.error(f"[planner] Credenciales revocadas para {user_email}: {exc}")
            error = "auth_revocada"
            events, emails, proximos = [], [], []
            alertas.append("🔑 Tu autorización de Google caducó o fue revocada, así que no "
                           "pude leer tu calendario ni tu correo. No es que no tengas nada "
                           "hoy: no puedo verlo. Reautoriza en https://amael-ia.richardx.dev "
                           "(botón «Calendar») y el brief de mañana sale normal.")

    # 3-4. Plan de bloques (solo con Google vivo y algo que planificar)
    plan_data: dict | None = None
    bloques: list[dict] = []
    tasks_created = 0
    plan_txt = ""
    if credentials and error is None:
        if not events and not emails and not tareas:
            plan_txt = "✅ Día libre: sin eventos, correos ni pendientes."
        else:
            perfil = await asyncio.to_thread(_perfil_de, user_email)
            prompt = _build_planning_prompt(
                date=hoy.strftime("%Y-%m-%d (%A)"), events=events, emails=emails,
                perfil=perfil, pendientes=_format_pendientes_prompt(tareas, fecha),
            )
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_get_llm().invoke, prompt)
                    raw = future.result(timeout=_LLM_WAIT_S)
                plan_data = _parse_plan(raw)
            except concurrent.futures.TimeoutError:
                logger.warning(f"[planner] LLM timeout para {user_email}")
                error = "llm_timeout"
                alertas.append("⚠️ El plan de bloques no se generó: el LLM tardó demasiado. "
                               "Lo demás del brief es de hoy.")
            except Exception as exc:
                logger.error(f"[planner] Error generando plan para {user_email}: {exc}")
                error = f"plan: {exc}"
                alertas.append(f"⚠️ El plan de bloques no se generó ({str(exc)[:80]}). "
                               "Lo demás del brief es de hoy.")

            if plan_data is not None:
                bloques, descartados = bb.validate_plan_tasks(plan_data.get("tasks") or [], events)
                # `reemplazar`: el plan del día es UNO — solo se tocan eventos con
                # la marca del planner, nunca los del usuario.
                tasks_created = sync_plan_to_calendar(
                    credentials, {"tasks": bloques, "date": fecha.isoformat()},
                    existentes=events, reemplazar=True,
                )
                plan_txt = bb.section_plan(plan_data, bloques)
                alertas += [f"Bloque descartado — {d}" for d in descartados]
                alertas += [str(w) for w in (plan_data.get("warnings") or [])]

    # 5. Ensamble
    summary = bb.assemble(
        today=fecha,
        section_today_txt=bb.section_today(events) if credentials and error != "auth_revocada" else "",
        upcoming_txt=bb.section_upcoming(proximos, fecha),
        due_txt=due_txt,
        pending_txt=pending_txt,
        plan_txt=plan_txt,
        alerts_txt=bb.section_alerts([], alertas),
    )
    logger.info(
        f"[planner] Brief para {user_email}: {tasks_created} bloques, {len(events)} eventos, "
        f"{len(proximos)} próximos, {len(emails)} emails, {len(tareas)} pendientes"
        + (f", error={error}" if error else "")
    )
    out: dict[str, Any] = {
        "summary":       summary,
        "tasks_created": tasks_created,
        "events_count":  len(events),
        "emails_count":  len(emails),
    }
    if error:
        out["error"] = error
    return out
