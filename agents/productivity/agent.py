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
from typing import Any, Dict

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger("agents.productivity.agent")

# Acciones soportadas como PRODUCTIVITY_TOOL
_SUPPORTED_ACTIONS = {
    "organize_day",
    "get_events",
    "get_emails",
    "credentials_status",
}


@AgentRegistry.register
class HanielAgent(BaseAgent):
    """
    Haniel — Productivity Agent: Google Calendar + Gmail + Vault.

    task dict esperado:
        {
            "action":     "organize_day" | "get_events" | "get_emails" | "credentials_status",
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
        "get_unread_emails",
        "sync_plan_to_calendar",
        "vault_oauth_credentials",
        "day_planner_cronjob",
    ]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
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
        return "organize_day"

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
            from agents.productivity.vault_credentials import get_user_credentials
            from agents.productivity.calendar_manager  import get_todays_events

            creds = get_user_credentials(user_email)
            if not creds:
                return AgentResult(
                    success=False,
                    output={"events": []},
                    agent_name=self.name,
                    error="no_credentials",
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
                error=str(exc),
            )

    async def _handle_get_emails(self, user_email: str) -> AgentResult:
        """Recupera los emails no leídos del inbox del usuario."""
        try:
            from agents.productivity.vault_credentials import get_user_credentials
            from agents.productivity.email_manager     import get_unread_emails

            creds = get_user_credentials(user_email)
            if not creds:
                return AgentResult(
                    success=False,
                    output={"emails": []},
                    agent_name=self.name,
                    error="no_credentials",
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
                error=str(exc),
            )

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
