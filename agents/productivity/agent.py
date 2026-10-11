"""
Productivity Agent — integración con Google Calendar/Gmail vía Vault OAuth.

Responsabilidades:
  - PRODUCTIVITY_TOOL: ejecuta acciones de calendar/email/planner
  - Organizacion del día (Day Planner CronJob + conversacional)
  - Gestión de credenciales OAuth via Vault KV v2

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("productivity", ctx)
"""
from __future__ import annotations

import logging
from typing import Any

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.productivity.agent")

# Acciones soportadas como PRODUCTIVITY_TOOL
_SUPPORTED_ACTIONS = {
    "organize_day",
    "get_events",
    "get_emails",
    "create_event",
    "credentials_status",
}

# Cuando Google rechaza el token, el usuario necesita saber QUÉ HACER, no el
# error crudo («invalid_grant: Token has been expired or revoked» — 10-oct).
_FRONTEND_URL = "https://amael-ia.richardx.dev"
_MSG_AUTH_REVOCADA = (
    "🔑 Tu acceso a Google Calendar/Gmail caducó o fue revocado. "
    f"Reautorízalo en {_FRONTEND_URL} (botón «Calendar») y vuelve a pedírmelo."
)
_MSG_SIN_CREDENCIALES = (
    "🔑 Aún no has conectado tu Google Calendar. "
    f"Conéctalo en {_FRONTEND_URL} (botón «Calendar») y vuelve a pedírmelo."
)


@AgentRegistry.register
class HanielAgent(BaseAgent):
    """
    Haniel — Productivity Agent: Google Calendar + Gmail + Vault.

    task dict esperado:
        {
            "action":     "organize_day" | "get_events" | "get_emails" | "create_event" | "credentials_status",
            "user_email": str,
            "query":      str,   # para contexto conversacional (opcional)
        }

    Todas las operaciones requieren que el usuario tenga credenciales OAuth
    almacenadas en Vault (ruta: secret/data/amael/google-tokens/{user}).
    """

    name         = "haniel"
    role         = "Productividad Personal — Google Calendar, Gmail, Day Planner"
    version      = "1.2.0"
    capabilities = [
        "organize_day",
        "get_calendar_events",
        "create_calendar_event",
        "get_unread_emails",
        "sync_plan_to_calendar",
        "vault_oauth_credentials",
        "day_planner_cronjob",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        action     = task.get("action", "").lower()
        user_email = task.get("user_email", "").strip()
        query      = task.get("query", "").strip()

        if not user_email:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error="user_email requerido para ProductivityAgent",
            )

        # Inferir acción del query si no se especifica explícitamente
        if not action and query:
            action = self._infer_action(query)

        if action == "organize_day":
            return await self._handle_organize_day(user_email)
        elif action == "get_events":
            return await self._handle_get_events(user_email)
        elif action == "get_emails":
            return await self._handle_get_emails(user_email)
        elif action == "create_event":
            return await self._handle_create_event(user_email, query)
        elif action == "credentials_status":
            return await self._handle_credentials_status(user_email)
        else:
            # Acción desconocida → organizar día como default
            logger.info(f"[productivity] Acción desconocida '{action}' → organize_day")
            return await self._handle_organize_day(user_email)

    def _infer_action(self, query: str) -> str:
        """Infiere la acción a partir del texto del query."""
        q = query.lower()
        if any(kw in q for kw in ["correo", "email", "gmail", "mensaje", "inbox"]):
            return "get_emails"
        if any(kw in q for kw in ["evento", "calendar", "agenda", "reunión", "cita"]):
            return "get_events"
        if any(kw in q for kw in ["organiza", "plan", "planifica", "día", "dia"]):
            return "organize_day"
        # Ante la duda, LEER. `organize_day` escribe eventos en el calendario
        # del usuario, y un «¿qué tengo pendiente?» —que no trae ninguna
        # palabra clave— le llenaba el día de bloques que nunca pidió.
        # Reorganizar el día ahora exige decirlo.
        return "get_events"

    async def _handle_organize_day(self, user_email: str) -> AgentResult:
        """Genera y sincroniza el plan del día completo."""
        try:
            from agents.productivity.day_planner import organize_day_for_user
            result = await organize_day_for_user(user_email)
            error  = result.get("error")
            return AgentResult(
                success=not bool(error),
                output=result,
                agent_name=self.name,
                error=error,
                metadata={
                    "tasks_created": result.get("tasks_created", 0),
                    "events_count":  result.get("events_count", 0),
                    "emails_count":  result.get("emails_count", 0),
                },
            )
        except Exception as exc:
            logger.error(f"[productivity] organize_day error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )

    async def _handle_get_events(self, user_email: str) -> AgentResult:
        """Recupera los eventos del día actual del usuario."""
        try:
            from agents.productivity.calendar_manager import get_todays_events
            from agents.productivity.vault_credentials import get_user_credentials

            creds = get_user_credentials(user_email)
            if not creds:
                return AgentResult(
                    success=False,
                    output={"events": []},
                    agent_name=self.name,
                    error=_MSG_SIN_CREDENCIALES,
                )
            events = get_todays_events(creds)
            return AgentResult(
                success=True,
                output={"events": events},
                agent_name=self.name,
                metadata={"count": len(events)},
            )
        except Exception as exc:
            logger.error(f"[productivity] get_events error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=self._error_usuario(exc),
            )

    async def _handle_get_emails(self, user_email: str) -> AgentResult:
        """Recupera los emails no leídos del inbox del usuario."""
        try:
            from agents.productivity.email_manager import get_unread_emails
            from agents.productivity.vault_credentials import get_user_credentials

            creds = get_user_credentials(user_email)
            if not creds:
                return AgentResult(
                    success=False,
                    output={"emails": []},
                    agent_name=self.name,
                    error=_MSG_SIN_CREDENCIALES,
                )
            emails = get_unread_emails(creds)
            return AgentResult(
                success=True,
                output={"emails": emails},
                agent_name=self.name,
                metadata={"count": len(emails)},
            )
        except Exception as exc:
            logger.error(f"[productivity] get_emails error: {exc}")
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=self._error_usuario(exc),
            )

    @staticmethod
    def _error_usuario(exc: Exception) -> str:
        from agents.productivity.errors import GoogleAuthRevocada
        return _MSG_AUTH_REVOCADA if isinstance(exc, GoogleAuthRevocada) else str(exc)

    async def _handle_create_event(self, user_email: str, query: str) -> AgentResult:
        """Crea un evento desde el chat: LLM extrae, código valida y crea,
        y la respuesta dice exactamente qué quedó agendado (ver
        event_extractor.py). Una sola repregunta fija si falta fecha u hora."""
        import asyncio
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from agents.productivity import calendar_manager, event_extractor
        from agents.productivity.vault_credentials import get_user_credentials

        if not query.strip():
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="Dime qué agendar (qué, cuándo y a qué hora).")
        try:
            creds = get_user_credentials(user_email)
            if not creds:
                return AgentResult(success=False, output=None, agent_name=self.name,
                                   error=_MSG_SIN_CREDENCIALES)

            tz = ZoneInfo("America/Mexico_City")
            now = datetime.now(tz)
            # El LLM es bloqueante: fuera del event loop (regla de la casa).
            parsed = await asyncio.to_thread(event_extractor.extract_event_fields, query, now)
            try:
                spec = event_extractor.resolve_event(parsed, query, now)
            except event_extractor.EventoIncompleto as inc:
                return AgentResult(success=True, agent_name=self.name,
                                   output={"response": event_extractor.format_incomplete(inc.faltan)})

            created = await asyncio.to_thread(
                calendar_manager.create_calendar_event,
                creds,
                summary=spec.title,
                start_iso=spec.start_iso,
                end_iso=spec.end_iso,
                description=spec.notes,
                location=spec.location,
                planner_mark=False,
            )
            if not created:
                return AgentResult(success=False, output=None, agent_name=self.name,
                                   error="Google Calendar no aceptó el evento; inténtalo de nuevo.")
            link = created.get("htmlLink", "") if isinstance(created, dict) else ""
            logger.info(f"[productivity] create_event ok {user_email} {spec.start_iso} {spec.title!r}")
            return AgentResult(
                success=True, agent_name=self.name,
                output={"response": event_extractor.format_confirmation(spec, link)},
                metadata={"start": spec.start_iso, "event_id": created.get("id") if isinstance(created, dict) else None},
            )
        except Exception as exc:
            logger.error(f"[productivity] create_event error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error=self._error_usuario(exc))

    async def _handle_credentials_status(self, user_email: str) -> AgentResult:
        """Verifica si el usuario tiene credenciales OAuth válidas en Vault."""
        try:
            from agents.productivity.vault_credentials import has_credentials
            connected = has_credentials(user_email)
            return AgentResult(
                success=True,
                output={"connected": connected, "user_email": user_email},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.error(f"[productivity] credentials_status error: {exc}")
            return AgentResult(
                success=False,
                output={"connected": False},
                agent_name=self.name,
                error=str(exc),
            )
