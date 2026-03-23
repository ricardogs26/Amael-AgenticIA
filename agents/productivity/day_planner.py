"""
Day Planner — genera y sincroniza el plan del día usando LLM + Google APIs.

Migrado desde productivity-service/app/services/planner_service.py:
  organize_day_for_user() — pipeline completo: credenciales → datos → LLM → calendario

Invocado por el CronJob `day-planner` cada weekday a las 7:00am (Mexico City).
También disponible para el agente conversacional vía PRODUCTIVITY_TOOL.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("agents.productivity.planner")

# Singleton LLM
_planner_llm = None


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
- Respetar los eventos ya agendados en el calendario
- Responde ÚNICAMENTE con el JSON, sin explicaciones adicionales.
""".strip()


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

    # 3. Generar plan con LLM
    today  = datetime.now().strftime("%Y-%m-%d (%A)")
    prompt = _PLANNING_PROMPT.format(
        date=today,
        events=_format_events(events),
        emails=_format_emails(emails),
    )

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_get_llm().invoke, prompt)
            raw    = future.result(timeout=60)

        plan_data = json.loads(raw)
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

    # 4. Sincronizar al calendario
    plan_data["date"] = datetime.now().strftime("%Y-%m-%d")
    tasks_created = sync_plan_to_calendar(credentials, plan_data)

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
