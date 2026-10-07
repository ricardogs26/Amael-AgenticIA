"""
CosyVoiceTool — síntesis de voz via cosyvoice-service.

Capacidades:
  synthesize(text, language)                   — genera audio WAV base64
  synthesize_and_send(text, phone, language)   — genera + envía nota de voz por WhatsApp

El cosyvoice-service corre en GPU desde 2.3.0 (7-oct-2026, ~1 s por segundo de
audio) y cae a CPU (~45 s por segundo) si no hay VRAM o la GPU falla; los topes de
este módulo siguen dimensionados para el peor caso (CPU).
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
    voice:    str | None = None   # WAV de /models/reference (cosyvoice ≥2.4.0); None = default

class SynthesizeAndSendInput(ToolInput):
    text:     str
    phone:    str | None = None   # Usa ADMIN_PHONE si no se especifica
    language: str = "es"
    voice:    str | None = None

class SynthesizeCloneInput(ToolInput):
    text:                   str
    reference_audio_base64: str
    prompt_text:            str
    language:               str = "es"

class SynthesizeCloneAndSendInput(ToolInput):
    text:                   str
    phone:                  str
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
        if isinstance(input, SynthesizeCloneAndSendInput):
            return await self.synthesize_clone_and_send(input)
        if isinstance(input, SynthesizeCloneInput):
            return await self.synthesize_clone(input)
        if isinstance(input, SynthesizeInput):
            return await self.synthesize(input)
        return ToolOutput.fail(
            f"Input tipo '{type(input).__name__}' no soportado",
            source=self.name,
        )

    async def device(self) -> str | None:
        """'cuda' | 'cpu' según /health de cosyvoice (≥2.3.0); None si no responde.

        Las respuestas en voz eligen motor con esto: en CPU una nota tarda
        minutos y conviene Piper.
        """
        import asyncio

        def _get() -> str | None:
            try:
                resp = _req.get(f"{_COSYVOICE_URL}/health", timeout=3)
                data = resp.json() if resp.status_code == 200 else {}
                return data.get("device") if data.get("status") == "ok" else None
            except Exception as exc:
                logger.warning(f"[cosyvoice_tool] /health no respondió: {exc}")
                return None

        return await asyncio.to_thread(_get)

    async def synthesize(self, input: SynthesizeInput) -> ToolOutput:
        """Genera audio WAV base64 desde texto."""
        import asyncio
        try:
            payload = {
                "text":     input.text[:500],
                "language": input.language,
                "speed":    input.speed,
            }
            if input.voice:
                payload["voice"] = input.voice
            # requests es bloqueante: dentro de un async congelaba el event loop
            # del backend mientras CosyVoice sintetizaba (minutos en CPU).
            resp = await asyncio.to_thread(
                _req.post, f"{_COSYVOICE_URL}/tts", json=payload, timeout=120,
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

    # Dimensionado para el respaldo en CPU (RTF ~12-45x): 300 chars ≈ 20s de
    # audio ≈ 4+ min de síntesis; en GPU son ~20 s. Cap corto + timeout amplio; siempre llamar vía asyncio.to_thread.
    _CLONE_MAX_CHARS = 300
    _CLONE_TIMEOUT_S = 480

    def _clone_sync(self, input: SynthesizeCloneInput) -> ToolOutput:
        """POST /tts/clone (bloqueante — usar desde to_thread)."""
        resp = _req.post(
            f"{_COSYVOICE_URL}/tts/clone",
            json={
                "text":                   input.text[: self._CLONE_MAX_CHARS],
                "reference_audio_base64": input.reference_audio_base64,
                "prompt_text":            input.prompt_text,
                "language":               input.language,
            },
            timeout=self._CLONE_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return ToolOutput.fail(
                f"cosyvoice-service HTTP {resp.status_code}: {resp.text[:200]}",
                source=self.name,
            )
        return ToolOutput.ok(data=resp.json(), source=self.name)

    async def synthesize_clone(self, input: SynthesizeCloneInput) -> ToolOutput:
        """Genera audio clonando la voz del audio de referencia (zero-shot)."""
        import asyncio
        try:
            return await asyncio.to_thread(self._clone_sync, input)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] synthesize_clone error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def synthesize_clone_and_send(
        self, input: SynthesizeCloneAndSendInput
    ) -> ToolOutput:
        """
        Clona voz + envía nota de voz por WhatsApp. Pipeline completo en un
        thread (síntesis CPU tarda minutos — no debe tocar el event loop):
          1. POST /tts/clone → WAV base64
          2. WAV → OGG OPUS (whatsapp-web.js truena con WAV grandes)
          3. POST /send-audio (ptt=True)
        """
        import asyncio

        def _pipeline() -> ToolOutput:
            clone = self._clone_sync(
                SynthesizeCloneInput(
                    text=input.text,
                    reference_audio_base64=input.reference_audio_base64,
                    prompt_text=input.prompt_text,
                    language=input.language,
                )
            )
            if not clone.success:
                return clone
            duration = clone.data.get("duration_seconds", 0)
            ogg_b64  = self._wav_to_ogg_opus(clone.data["audio_base64"])
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send-audio",
                json={
                    "phoneNumber": input.phone,
                    "base64":      ogg_b64,
                    "mimetype":    "audio/ogg; codecs=opus",
                    "ptt":         True,
                },
                timeout=45,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge /send-audio HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            logger.info(
                f"[cosyvoice_tool] Nota de voz CLONADA enviada a {input.phone} "
                f"({duration:.1f}s, {len(input.text)} chars)"
            )
            return ToolOutput.ok(
                data={"sent": True, "phone": input.phone,
                      "duration_seconds": duration, "cloned": True},
                source=self.name,
            )

        try:
            return await asyncio.to_thread(_pipeline)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] synthesize_clone_and_send error: {exc}")
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
            SynthesizeInput(text=input.text, language=input.language, voice=input.voice)
        )
        if not synth_result.success:
            return synth_result

        wav_b64  = synth_result.data["audio_base64"]
        duration = synth_result.data.get("duration_seconds", 0)

        # 2. Convertir WAV → OGG OPUS (ffmpeg es bloqueante: fuera del event loop)
        import asyncio
        try:
            ogg_b64 = await asyncio.to_thread(self._wav_to_ogg_opus, wav_b64)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] WAV→OGG error: {exc}")
            return ToolOutput.fail(f"Error convirtiendo audio: {exc}", source=self.name)

        # 3. Enviar al bridge como nota de voz PTT (OGG OPUS)
        try:
            resp = await asyncio.to_thread(
                _req.post,
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

    async def speak(
        self,
        text: str,
        phone: str,
        *,
        voice: str | None = None,
        clone_ref: tuple[str, str] | None = None,
        language: str = "es",
    ) -> ToolOutput:
        """Lee `text` en una sola nota de voz, por largo que sea (idea 3).

        Recorta al tope del dispositivo en fin de frase (2 500 chars en GPU,
        500 en CPU), parte en fragmentos de ≤450 (cosyvoice acepta 500), los
        sintetiza en orden —con la voz clonada si viene `clone_ref`
        (wav_b64, transcripción), si no con `voice`— y une los WAV.
        Todo el pipeline corre en un hilo: nada bloquea el event loop.
        """
        import asyncio

        from tools.cosyvoice import chunking

        phone = phone or _ADMIN_PHONE
        if not phone:
            return ToolOutput.fail("Sin número destino", source=self.name)
        device = await self.device()
        body = chunking.truncate_at_sentence(text, chunking.max_chars_for(device))
        chunks = chunking.split_for_tts(body)
        if not chunks:
            return ToolOutput.fail("Texto vacío", source=self.name)

        def _one(chunk: str) -> bytes:
            if clone_ref:
                url = f"{_COSYVOICE_URL}/tts/clone"
                payload = {"text": chunk, "reference_audio_base64": clone_ref[0],
                           "prompt_text": clone_ref[1], "language": language}
                timeout = self._CLONE_TIMEOUT_S
            else:
                url = f"{_COSYVOICE_URL}/tts"
                payload = {"text": chunk, "language": language}
                if voice:
                    payload["voice"] = voice
                timeout = 120 if device == "cuda" else self._CLONE_TIMEOUT_S
            resp = _req.post(url, json=payload, timeout=timeout)
            if resp.status_code != 200:
                raise RuntimeError(f"cosyvoice-service HTTP {resp.status_code}: {resp.text[:200]}")
            return base64.b64decode(resp.json()["audio_base64"])

        def _pipeline() -> ToolOutput:
            wavs = [_one(c) for c in chunks]
            wav = chunking.concat_wavs(wavs) if len(wavs) > 1 else wavs[0]
            ogg_b64 = self._wav_to_ogg_opus(base64.b64encode(wav).decode())
            resp = _req.post(
                f"{_WA_BRIDGE_URL}/send-audio",
                json={"phoneNumber": phone, "base64": ogg_b64,
                      "mimetype": "audio/ogg; codecs=opus", "ptt": True},
                timeout=60,
            )
            if resp.status_code not in (200, 201):
                return ToolOutput.fail(
                    f"whatsapp-bridge /send-audio HTTP {resp.status_code}: {resp.text[:200]}",
                    source=self.name,
                )
            import io as _io
            import wave as _wave
            with _wave.open(_io.BytesIO(wav), "rb") as w:
                duration = w.getnframes() / w.getframerate()
            logger.info(
                f"[cosyvoice_tool] Nota de voz enviada a {phone} ({duration:.1f}s, "
                f"{len(body)} chars en {len(chunks)} fragmento(s), "
                f"{'clonada' if clone_ref else voice or 'default'}, {device})"
            )
            return ToolOutput.ok(
                data={"sent": True, "phone": phone, "duration_seconds": duration,
                      "chars": len(body), "chunks": len(chunks), "truncated": len(body) < len(text.strip())},
                source=self.name,
            )

        try:
            return await asyncio.to_thread(_pipeline)
        except Exception as exc:
            logger.error(f"[cosyvoice_tool] speak error: {exc}")
            return ToolOutput.fail(str(exc), source=self.name)

    async def health_check(self) -> bool:
        """Verifica que CosyVoice responde (non-blocking)."""
        import asyncio

        def _check() -> bool:
            try:
                resp = _req.get(f"{_COSYVOICE_URL}/health", timeout=5)
                return resp.status_code == 200 and resp.json().get("status") == "ok"
            except Exception as exc:
                logger.warning(f"[cosyvoice_tool] health_check falló: {exc}")
                return False

        return await asyncio.to_thread(_check)
