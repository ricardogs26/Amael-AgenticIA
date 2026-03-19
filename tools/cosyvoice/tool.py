"""
CosyVoiceTool — síntesis de voz via cosyvoice-service.

Capacidades:
  synthesize(text, language)                   — genera audio WAV base64
  synthesize_and_send(text, phone, language)   — genera + envía nota de voz por WhatsApp

El cosyvoice-service corre en CPU (sin GPU) en el mismo cluster.
El whatsapp-bridge acepta audio vía /send-audio (OGG OPUS, ptt=True).
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess

import requests as _req

from core.tool_base import BaseTool, ToolInput, ToolOutput
from tools.registry import ToolRegistry

logger = logging.getLogger("tool.cosyvoice")

_COSYVOICE_URL = os.environ.get(
    "COSYVOICE_SERVICE_URL",
    "http://cosyvoice-service:8010",
)
_WA_BRIDGE_URL = os.environ.get(
    "WHATSAPP_BRIDGE_URL",
    "http://whatsapp-bridge-service.amael-ia.svc.cluster.local:3000",
)
_ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "")


# ── Inputs ────────────────────────────────────────────────────────────────────

class SynthesizeInput(ToolInput):
    text:     str
    language: str = "es"
    speed:    float = 1.0

class SynthesizeAndSendInput(ToolInput):
    text:     str
    phone:    str | None = None   # Usa ADMIN_PHONE si no se especifica
    language: str = "es"

class SynthesizeCloneInput(ToolInput):
    text:                   str
    reference_audio_base64: str
    prompt_text:            str
    language:               str = "es"


# ── Tool ──────────────────────────────────────────────────────────────────────

@ToolRegistry.register
class CosyVoiceTool(BaseTool):
    """
    Síntesis de voz (TTS) via cosyvoice-service.
    Soporta síntesis estándar y zero-shot voice cloning.
    Integrado con WhatsApp para enviar notas de voz (PTT).
    """

    name            = "cosyvoice"
    description     = "Síntesis de voz TTS y envío de notas de voz por WhatsApp"
    version         = "1.0.0"
    external_system = "cosyvoice-service"

    async def execute(self, input: ToolInput) -> ToolOutput:
        if isinstance(input, SynthesizeAndSendInput):
            return await self.synthesize_and_send(input)
        if isinstance(input, SynthesizeCloneInput):
            return await self.synthesize_clone(input)
        if isinstance(input, SynthesizeInput):
            return await self.synthesize(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def synthesize(self, input: SynthesizeInput) -> ToolOutput:
        """Genera audio WAV base64 desde texto."""
        try:
            resp = _req.post(
                f"{_COSYVOICE_URL}/tts",
                json={
                    "text":     input.text[:500],
                    "language": input.language,
                    "speed":    input.speed,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"cosyvoice-service HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            data = resp.json()
            return ToolOutput.ok(
                data={
                    "audio_base64":     data["audio_base64"],
                    "format":           data.get("format", "wav"),
                    "sample_rate":      data.get("sample_rate", 22050),
                    "duration_seconds": data.get("duration_seconds", 0),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] synthesize error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def synthesize_clone(self, input: SynthesizeCloneInput) -> ToolOutput:
        """Genera audio clonando la voz del audio de referencia (zero-shot)."""
        try:
            resp = _req.post(
                f"{_COSYVOICE_URL}/tts/clone",
                json={
                    "text":                   input.text[:500],
                    "reference_audio_base64": input.reference_audio_base64,
                    "prompt_text":            input.prompt_text,
                    "language":               input.language,
                },
                timeout=90,
            )
            if resp.status_code != 200:
                return ToolOutput.fail(
                    f"cosyvoice-service HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            return ToolOutput.ok(data=resp.json(), source=self.name)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] synthesize_clone error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    @staticmethod
    def _wav_to_ogg_opus(wav_b64: str) -> str:
        """
        Convierte audio WAV base64 → OGG OPUS base64 usando ffmpeg.
        WhatsApp requiere OGG OPUS para notas de voz PTT.
        """
        wav_bytes = base64.b64decode(wav_b64)
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "wav", "-i", "pipe:0",
                "-c:a", "libopus",
                "-b:a", "24k",
                "-vbr", "on",
                "-compression_level", "10",
                "-f", "ogg",
                "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg WAV→OGG error: {result.stderr.decode(errors='replace')[:200]}"
            )
        return base64.b64encode(result.stdout).decode()

    async def synthesize_and_send(self, input: SynthesizeAndSendInput) -> ToolOutput:
        """
        Genera audio y lo envía como nota de voz por WhatsApp.

        Flujo:
          1. POST /tts → base64 WAV
          2. Convierte WAV → OGG OPUS (WhatsApp solo acepta OGG OPUS como PTT)
          3. POST /send-audio en whatsapp-bridge → nota de voz PTT
        """
        phone = input.phone or _ADMIN_PHONE
        if not phone:
            return ToolOutput.fail(
                "No hay número destino (ADMIN_PHONE no configurado)",
                source=self.name,
            )

        # 1. Síntesis
        synth_result = await self.synthesize(
            SynthesizeInput(text=input.text, language=input.language)
        )
        if not synth_result.success:
            return synth_result

        wav_b64  = synth_result.data["audio_base64"]
        duration = synth_result.data.get("duration_seconds", 0)

        # 2. Convertir WAV → OGG OPUS
        try:
            ogg_b64 = self._wav_to_ogg_opus(wav_b64)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] WAV→OGG error: {exc}")
            return ToolOutput.fail(f"Error convirtiendo audio: {exc}", source=self.name)

        # 3. Enviar al bridge como nota de voz PTT (OGG OPUS)
        try:
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send-audio",
                json={
                    "phoneNumber": phone,
                    "base64":      ogg_b64,
                    "mimetype":    "audio/ogg; codecs=opus",
                    "ptt":         True,
                },
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge /send-audio HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            logger.info(
                f"[cosyvoice_tool] Nota de voz enviada a {phone} "
                f"({duration:.1f}s, {len(input.text)} chars)"
            )
            return ToolOutput.ok(
                data={
                    "sent":             True,
                    "phone":            phone,
                    "duration_seconds": duration,
                    "chars":            len(input.text),
                },
                source=self.name,
            )
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] send-audio error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def health_check(self) -> bool:
        try:
            resp = _req.get(f"{_COSYVOICE_URL}/health", timeout=5)
            return resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception as exc:
            logger.warning(f"[cosyvoice_tool] health_check falló: {exc}")
            return False
