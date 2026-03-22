"""
WhatsAppTool — envío de mensajes y media vía whatsapp-bridge.

Capacidades:
  send_text(phone, message)                   — texto plano
  send_media(phone, base64_image, caption)    — imagen + caption
  send_sre_alert(message, severity)           — alerta SRE con pacing de 3s

Migrado desde k8s-agent/main.py → notify_whatsapp_sre() + _send_sre_notification().

Pacing: Las alertas SRE respetan un rate limit de 3 segundos entre mensajes
para evitar flood en WhatsApp y proteger el bridge Puppeteer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.whatsapp")

_WA_BRIDGE_URL = os.environ.get(
    "WHATSAPP_BRIDGE_URL",
    "http://whatsapp-bridge-service.amael-ia.svc.cluster.local:3000",
)

# Número destino por defecto para alertas SRE
_SRE_ALERT_PHONE = os.environ.get("SRE_ALERT_PHONE", "")

# Umbral de severidad mínima para enviar alerta (LOW / MEDIUM / HIGH / CRITICAL)
_SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_MIN_ALERT_SEVERITY = os.environ.get("SRE_MIN_ALERT_SEVERITY", "MEDIUM").upper()

# Pacing entre alertas SRE
_SRE_ALERT_PACE_SECONDS = 3.0
_last_alert_ts: float = 0.0


# ── Inputs ────────────────────────────────────────────────────────────────────

class SendTextInput(ToolInput):
    phone:   str
    message: str

class SendMediaInput(ToolInput):
    phone:   str
    base64_image: str    # base64-encoded PNG/JPEG
    caption: str = ""

class SendSREAlertInput(ToolInput):
    message:      str
    severity:     str = "HIGH"       # LOW | MEDIUM | HIGH | CRITICAL
    phone:        str | None = None   # Sobreescribe _SRE_ALERT_PHONE si se da
    min_severity: str | None = None  # Sobreescribe _MIN_ALERT_SEVERITY si se da


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class WhatsAppTool(BaseTool):
    """
    Envío de mensajes WhatsApp via el whatsapp-bridge (Puppeteer + WhatsApp Web).
    Usada por SREAgent para notificaciones de anomalías y alertas.
    """

    name            = "whatsapp"
    description     = "Envío de mensajes y alertas vía WhatsApp Bridge"
    version         = "1.0.0"
    external_system = "whatsapp"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, SendTextInput):
            return await self.send_text(input)
        if isinstance(input, SendMediaInput):
            return await self.send_media(input)
        if isinstance(input, SendSREAlertInput):
            return await self.send_sre_alert(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def send_text(self, input: SendTextInput) -> ToolOutput:
        """Envía un mensaje de texto al número indicado."""
        try:
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send",
                json={"phone": input.phone, "message": input.message},
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            return ToolOutput.ok(
                data={"phone": input.phone, "sent": True},
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[whatsapp_tool] send_text error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def send_media(self, input: SendMediaInput) -> ToolOutput:
        """Envía una imagen base64 con caption al número indicado."""
        try:
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send-media",
                json={
                    "phone":   input.phone,
                    "image":   input.base64_image,
                    "caption": input.caption,
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            return ToolOutput.ok(
                data={"phone": input.phone, "sent": True, "has_media": True},
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[whatsapp_tool] send_media error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def send_sre_alert(self, input: SendSREAlertInput) -> ToolOutput:
        """
        Envía una alerta SRE con pacing y filtro de severidad mínima.

        - Aplica umbral de severidad: si severity < min_severity, skips silenciosamente.
        - Respeta pacing de 3s entre alertas para no saturar el bridge.
        - El número destino se toma de input.phone > env SRE_ALERT_PHONE.
        """
        global _last_alert_ts

        severity     = input.severity.upper()
        min_severity = (input.min_severity or _MIN_ALERT_SEVERITY).upper()
        phone        = input.phone or _SRE_ALERT_PHONE

        if not phone:
            return ToolOutput.fail(
                "No hay número destino configurado (SRE_ALERT_PHONE no definido)",
                source=self.name,
            )

        # Filtro de severidad
        sev_level = _SEVERITY_ORDER.get(severity, 1)
        min_level = _SEVERITY_ORDER.get(min_severity, 1)
        if sev_level < min_level:
            logger.debug(
                f"[whatsapp_tool] Alerta {severity} ignorada "
                f"(umbral mínimo: {min_severity})"
            )
            return ToolOutput.ok(
                data={"skipped": True, "reason": f"severity {severity} < min {min_severity}"},
                source=self.name,
            )

        # Pacing: esperar si el último envío fue muy reciente
        now     = time.monotonic()
        elapsed = now - _last_alert_ts
        if elapsed < _SRE_ALERT_PACE_SECONDS:
            wait = _SRE_ALERT_PACE_SECONDS - elapsed
            logger.debug(f"[whatsapp_tool] Pacing: esperando {wait:.1f}s")
            await asyncio.sleep(wait)

        # Formatear mensaje con ícono de severidad
        _icons = {"LOW": "ℹ️", "MEDIUM": "⚠️", "HIGH": "🔴", "CRITICAL": "🚨"}
        icon    = _icons.get(severity, "⚠️")
        message = f"{icon} *[SRE {severity}]*\n{input.message}"

        result = await self.send_text(SendTextInput(phone=phone, message=message))
        if result.success:
            _last_alert_ts = time.monotonic()

        return result

    async def health_check(self) -> bool:
        """Verifica que el whatsapp-bridge responde en /health o / (non-blocking)."""
        import asyncio

        def _check() -> bool:
            try:
                resp = _req.get(f"{_WA_BRIDGE_URL}/health", timeout=5)
                return resp.status_code in (200, 404)  # 404 = bridge vivo pero sin /health
            except Exception as exc:
                logger.warning(f"[whatsapp_tool] health_check falló: {exc}")
                return False

        return await asyncio.to_thread(_check)
