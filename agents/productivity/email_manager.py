"""
Email Manager — lectura de emails no leídos via Gmail API.

Migrado desde productivity-service/app/services/google_apis.py:
  get_unread_emails() — lista los últimos N emails no leídos del inbox

Solo operaciones de lectura (scope: gmail.readonly).
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List

logger = logging.getLogger("agents.productivity.email")


def _build_gmail_service(credentials):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=credentials)


def _decode_body(data: str) -> str:
    """Decodifica body en base64url de Gmail."""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_unread_emails(credentials, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Recupera los últimos N emails no leídos del inbox del usuario.

    Returns:
        Lista de dicts con keys: subject, from, date, snippet, body_preview.
    Migrado desde productivity-service/app/services/google_apis.py → get_unread_emails()
    """
    try:
        service = _build_gmail_service(credentials)
        result  = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                maxResults=max_results,
            )
            .execute()
        )
        messages = result.get("messages", [])
        emails   = []

        for msg_ref in messages:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="metadata",
                         metadataHeaders=["Subject", "From", "Date"])
                    .execute()
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                emails.append({
                    "subject":       headers.get("subject", "(sin asunto)"),
                    "from":          headers.get("from", ""),
                    "date":          headers.get("date", ""),
                    "snippet":       msg.get("snippet", ""),
                    "body_preview":  msg.get("snippet", "")[:300],
                })
            except Exception as exc:
                logger.warning(f"[email] Error leyendo mensaje {msg_ref['id']}: {exc}")

        logger.info(f"[email] {len(emails)} emails no leídos recuperados.")
        return emails

    except Exception as exc:
        logger.error(f"[email] get_unread_emails error: {exc}")
        return []
