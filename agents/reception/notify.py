"""Envíos por el bridge: al admin (Ricardo) y al visitante."""
from __future__ import annotations

import logging

logger = logging.getLogger("agents.reception.notify")


def _post(phone: str, text: str) -> None:
    import httpx

    from config.settings import settings

    resp = httpx.post(
        f"{settings.whatsapp_bridge_url}/send",
        json={"phoneNumber": phone, "text": text},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"bridge /send respondió {resp.status_code}")


def send_to_admin(text: str) -> None:
    from config.settings import settings
    _post(settings.admin_phone, text)


def send_to_visitor(phone: str, text: str) -> None:
    _post(phone, text)
