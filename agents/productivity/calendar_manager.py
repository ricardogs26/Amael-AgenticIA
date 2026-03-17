"""
Calendar Manager — operaciones sobre Google Calendar via API.

Migrado desde productivity-service/app/services/:
  get_todays_events()   — lista eventos del día actual del usuario
  create_calendar_event() — crea evento en el calendario
  sync_plan_to_calendar() — sincroniza un plan de día al calendario
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("agents.productivity.calendar")


def _build_calendar_service(credentials):
    """Construye el cliente de Google Calendar API."""
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=credentials)


def get_todays_events(credentials) -> list[dict[str, Any]]:
    """
    Recupera los eventos del día actual del calendario del usuario.

    Returns:
        Lista de dicts con keys: summary, start, end, description, location.
    """
    try:
        service = _build_calendar_service(credentials)
        now     = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0).isoformat()
        end_of_day   = now.replace(hour=23, minute=59, second=59).isoformat()

        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_of_day,
                timeMax=end_of_day,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = result.get("items", [])
        logger.info(f"[calendar] {len(events)} eventos hoy.")
        return [
            {
                "summary":     e.get("summary", "(sin título)"),
                "start":       e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
                "end":         e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
                "description": e.get("description", ""),
                "location":    e.get("location", ""),
            }
            for e in events
        ]
    except Exception as exc:
        logger.error(f"[calendar] get_todays_events error: {exc}")
        return []


def create_calendar_event(
    credentials,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    location: str = "",
    timezone_id: str = "America/Mexico_City",
) -> dict[str, Any] | None:
    """
    Crea un evento en el calendario principal del usuario.

    Args:
        credentials: Google OAuth Credentials.
        summary:     Título del evento.
        start_iso:   Inicio en formato ISO 8601 (ej. '2026-03-15T09:00:00').
        end_iso:     Fin en formato ISO 8601.
        description: Descripción opcional.
        location:    Ubicación opcional.
        timezone_id: Zona horaria (default: America/Mexico_City).

    Returns:
        Evento creado (dict) o None si hubo error.
    """
    try:
        service = _build_calendar_service(credentials)
        event   = {
            "summary":     summary,
            "description": description,
            "location":    location,
            "start": {"dateTime": start_iso, "timeZone": timezone_id},
            "end":   {"dateTime": end_iso,   "timeZone": timezone_id},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        logger.info(f"[calendar] Evento creado: {summary!r} ({start_iso})")
        return created
    except Exception as exc:
        logger.error(f"[calendar] create_calendar_event error: {exc}")
        return None


def sync_plan_to_calendar(credentials, plan_data: dict[str, Any]) -> int:
    """
    Sincroniza un plan de día (generado por LLM) al calendario del usuario.

    plan_data esperado:
        {
          "tasks": [
            {"title": str, "start": "HH:MM", "end": "HH:MM", "description": str},
            ...
          ],
          "date": "YYYY-MM-DD"   # opcional, default: hoy
        }

    Returns:
        Número de eventos creados exitosamente.
    Migrado desde productivity-service/app/services/planner_service.py → sync_plan_to_calendar()
    """
    tasks = plan_data.get("tasks", [])
    date  = plan_data.get("date") or datetime.now().strftime("%Y-%m-%d")
    created = 0

    for task in tasks:
        title       = task.get("title", "(tarea)")
        start_time  = task.get("start", "09:00")
        end_time    = task.get("end", "09:30")
        description = task.get("description", "")

        start_iso = f"{date}T{start_time}:00"
        end_iso   = f"{date}T{end_time}:00"

        event = create_calendar_event(
            credentials,
            summary=title,
            start_iso=start_iso,
            end_iso=end_iso,
            description=description,
        )
        if event:
            created += 1

    logger.info(f"[calendar] {created}/{len(tasks)} tareas sincronizadas al calendario.")
    return created
