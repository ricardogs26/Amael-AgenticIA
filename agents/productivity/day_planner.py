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
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger("agents.productivity.planner")

# Zona horaria por defecto cuando el perfil del usuario no dice otra cosa. El
# planner corría con la hora del POD (UTC): a las 7:00 de México ya era el día
# siguiente en UTC durante parte del año, así que el plan podía fecharse mal.
_TZ_DEFAULT = "America/Mexico_City"

# Singleton LLM
_planner_llm = None


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
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"sin JSON en la respuesta del LLM: {text[:200]}")
    return json.loads(m.group(0))


def _get_llm():
    global _planner_llm
    if _planner_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _planner_llm = get_chat_llm(timeout=60)
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
- Responde ÚNICAMENTE con el JSON, sin explicaciones adicionales.
""".strip()


def _build_planning_prompt(date: str, events: list, emails: list, perfil: str = "") -> str:
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
    return _PLANNING_PROMPT.format(
        date=date,
        profile=bloque,
        events=_format_events(events),
        emails=_format_emails(emails),
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
    Pipeline completo de organización del día para un usuario:
      1. Obtiene credenciales Google desde Vault
      2. Recupera eventos del calendario y emails no leídos
      3. Genera plan con LLM
      4. Sincroniza al calendario
      5. Retorna resumen

    Migrado desde productivity-service/app/services/planner_service.py → organize_day_for_user()
    """
    from agents.productivity.calendar_manager import get_todays_events, sync_plan_to_calendar
    from agents.productivity.email_manager import get_unread_emails
    from agents.productivity.vault_credentials import get_user_credentials

    # 1. Credenciales
    credentials = get_user_credentials(user_email)
    if not credentials:
        return {
            "summary": (
                "❌ No se encontraron credenciales de Google Calendar para este usuario. "
                "Por favor, autoriza el acceso en /api/auth/calendar"
            ),
            "tasks_created": 0,
            "error": "no_credentials",
        }

    # 2. Datos
    events = get_todays_events(credentials)
    emails = get_unread_emails(credentials)

    if not events and not emails:
        return {
            "summary": "✅ ¡Tu día está libre! No hay eventos ni emails pendientes.",
            "tasks_created": 0,
        }

    # 3. Generar plan con LLM, con los hechos que la memoria sabe del usuario
    perfil = await asyncio.to_thread(_perfil_de, user_email)
    hoy    = _hoy_para(user_email)
    today  = hoy.strftime("%Y-%m-%d (%A)")
    prompt = _build_planning_prompt(date=today, events=events, emails=emails, perfil=perfil)

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_get_llm().invoke, prompt)
            raw    = future.result(timeout=60)

        plan_data = _parse_plan(raw)
    except concurrent.futures.TimeoutError:
        logger.warning(f"[planner] LLM timeout para {user_email}")
        return {
            "summary": "⚠️ El LLM tardó demasiado en generar el plan. Intenta más tarde.",
            "tasks_created": 0,
            "error": "llm_timeout",
        }
    except (json.JSONDecodeError, Exception) as exc:
        logger.error(f"[planner] Error generando plan para {user_email}: {exc}")
        return {
            "summary": f"❌ No se pudo generar un plan válido: {exc}",
            "tasks_created": 0,
            "error": str(exc),
        }

    # 4. Sincronizar al calendario, sin recrear lo que el usuario ya tiene
    plan_data["date"] = hoy.strftime("%Y-%m-%d")
    tasks_created = sync_plan_to_calendar(credentials, plan_data, existentes=events)

    # 5. Resumen
    summary      = plan_data.get("summary", "Plan generado.")
    priorities   = plan_data.get("priorities", [])
    warnings     = plan_data.get("warnings", [])

    full_summary = summary
    if priorities:
        full_summary += "\n\n**Prioridades:**\n" + "\n".join(f"• {p}" for p in priorities)
    if warnings:
        full_summary += "\n\n**⚠️ Alertas:**\n" + "\n".join(f"• {w}" for w in warnings)

    logger.info(
        f"[planner] Plan generado para {user_email}: "
        f"{tasks_created} tareas, {len(events)} eventos, {len(emails)} emails"
    )
    return {
        "summary":       full_summary,
        "tasks_created": tasks_created,
        "events_count":  len(events),
        "emails_count":  len(emails),
    }
