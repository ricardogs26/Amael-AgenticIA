"""
agents.productivity — Productivity Agent package.

Módulos:
  vault_credentials — OAuth tokens de Google en Vault KV v2
  calendar_manager  — Google Calendar: leer eventos, crear eventos, sync plan
  email_manager     — Gmail: emails no leídos (readonly)
  day_planner       — Pipeline LLM: eventos + emails → plan → calendario
  agent             — ProductivityAgent(BaseAgent) registrado en AgentRegistry
"""
from agents.productivity.agent import HanielAgent as ProductivityAgent
from agents.productivity.calendar_manager import (
    create_calendar_event,
    get_todays_events,
    sync_plan_to_calendar,
)
from agents.productivity.day_planner import organize_day_for_user
from agents.productivity.email_manager import get_unread_emails
from agents.productivity.vault_credentials import (
    get_auth_flow,
    get_user_credentials,
    has_credentials,
    save_user_credentials,
)

__all__ = [
    "ProductivityAgent",
    "organize_day_for_user",
    "get_user_credentials",
    "save_user_credentials",
    "has_credentials",
    "get_auth_flow",
    "get_todays_events",
    "create_calendar_event",
    "sync_plan_to_calendar",
    "get_unread_emails",
]
