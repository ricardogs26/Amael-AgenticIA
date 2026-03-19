"""
PiperTool — síntesis de voz via piper-service.

Capacidades:
  synthesize(text, voice, speed)              — genera audio WAV base64
  synthesize_and_send(text, phone, voice)     — genera + envía nota de voz WhatsApp
"""
from __future__ import annotations

import logging
import os

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.piper")

_PIPER_URL    = os.environ.get("PIPER_SERVICE_URL", "http://piper-service:8010")
_WA_BRIDGE_URL = os.environ.get(
    "WHATSAPP_BRIDGE_URL",
    "http://whatsapp-bridge-service.amael-ia.svc.cluster.local:3000",
)
_ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")


# ── Inputs ────────────────────────────────────────────────────────────────────

class SynthesizeInput(ToolInput):
    text:  str
    voice: str | None = None    # None → usa default del servicio (es_MX-claude-high)
    speed: float = 1.0

class SynthesizeAndSendInput(ToolInput):
    text:  str
    phone: str | None = None    # None → usa ADMIN_PHONE
    voice: str | None = None
    speed: float = 1.0


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class PiperTool(BaseTool):
    """
    Síntesis de voz TTS via piper-service (Piper TTS).
    Latencia ~50ms en CPU. Soporta es_MX y en_US.
    Integrado con WhatsApp para enviar notas de voz (PTT).
    """

    name            = "piper"
    description     = "Síntesis de voz TTS rápida y envío de notas de voz por WhatsApp"
    version         = "1.0.0"
    external_system = "piper-service"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, SynthesizeAndSendInput):
            return await self.synthesize_and_send(input)
        if isinstance(input, SynthesizeInput):
            return await self.synthesize(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def synthesize(self, input: SynthesizeInput) -> ToolOutput:
        """Genera audio WAV base64 desde texto."""
        try:
            payload: dict = {"text": input.text[:1000], "speed": input.speed}
            if input.voice:
                payload["voice"] = input.voice

            resp = _req.post(f"{_PIPER_URL}/tts", json=payload, timeout=30)
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"piper-service HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            data = resp.json()
            return ToolOutput.ok(
                data={
                    "audio_base64":     data["audio_base64"],
                    "format":           data.get("format", "wav"),
                    "voice":            data.get("voice", "unknown"),
                    "duration_seconds": data.get("duration_seconds", 0),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[piper_tool] synthesize error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def synthesize_and_send(self, input: SynthesizeAndSendInput) -> ToolOutput:
        """Genera audio y lo envía como nota de voz PTT por WhatsApp."""
        phone = input.phone or _ADMIN_PHONE
        if not phone:
            return ToolOutput.fail(
                "No hay número destino (ADMIN_PHONE no configurado)",
                source=self.name,
            )

        # 1. Síntesis
        synth = await self.synthesize(
            SynthesizeInput(text=input.text, voice=input.voice, speed=input.speed)
        )
        if not synth.success:
            return synth

        # 2. Enviar al bridge como nota de voz PTT
        try:
            mimetype = synth.data.get("mimetype", "audio/ogg; codecs=opus")
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send-audio",
                json={
                    "phoneNumber": phone,
                    "base64":      synth.data["audio_base64"],
                    "mimetype":    mimetype,
                    "ptt":         True,
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge /send-audio HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            duration = synth.data.get("duration_seconds", 0)
            voice    = synth.data.get("voice", "unknown")
            logger.info(
                f"[piper_tool] Nota de voz enviada | phone={phone} "
                f"voice={voice} duration={duration:.1f}s"
            )
            return ToolOutput.ok(
                data={
                    "sent":             True,
                    "phone":            phone,
                    "voice":            voice,
                    "duration_seconds": duration,
                    "chars":            len(input.text),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[piper_tool] send-audio error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def health_check(self) -> bool:
        try:
            resp = _req.get(f"{_PIPER_URL}/health", timeout=5)
            return resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception as exc:
            logger.warning(f"[piper_tool] health_check falló: {exc}")
            return False
