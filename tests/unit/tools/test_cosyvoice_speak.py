"""CosyVoiceTool.speak(): textos largos en una sola nota (idea 3)."""
from __future__ import annotations

import base64
import io
import wave
from types import SimpleNamespace

import pytest

from tools.cosyvoice import tool as cv


def _wav_b64(seconds=1.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def red(monkeypatch):
    posts = []

    def post(url, json=None, timeout=None):
        posts.append((url, json))
        if url.endswith("/send-audio"):
            return SimpleNamespace(status_code=200, text="ok")
        return SimpleNamespace(status_code=200, json=lambda: {"audio_base64": _wav_b64(1.0)})

    monkeypatch.setattr(cv._req, "post", post)
    monkeypatch.setattr(cv.CosyVoiceTool, "_wav_to_ogg_opus", staticmethod(lambda b: "OGG"))
    return posts


def _device(monkeypatch, dev):
    async def device(self):
        return dev
    monkeypatch.setattr(cv.CosyVoiceTool, "device", device)


async def test_texto_largo_varios_fragmentos_una_sola_nota(red, monkeypatch):
    _device(monkeypatch, "cuda")
    texto = "Esta es una frase del correo de hoy. " * 40   # ~1 480 chars → 4 fragmentos
    r = await cv.CosyVoiceTool().speak(texto, "5215550001111", voice="es_MX_female")
    tts = [j for u, j in red if u.endswith("/tts")]
    sends = [u for u, _ in red if u.endswith("/send-audio")]
    assert r.success and len(tts) == r.data["chunks"] >= 3
    assert all(j["voice"] == "es_MX_female" and len(j["text"]) <= 450 for j in tts)
    assert len(sends) == 1, "una sola nota de voz"
    assert r.data["duration_seconds"] == pytest.approx(len(tts) * 1.0 + (len(tts) - 1) * 0.15, abs=0.01)


async def test_voz_clonada_va_a_tts_clone(red, monkeypatch):
    _device(monkeypatch, "cuda")
    r = await cv.CosyVoiceTool().speak("Hola Ricardo.", "5215550001111", clone_ref=("REF", "transcripción"))
    clone = [j for u, j in red if u.endswith("/tts/clone")]
    assert r.success and clone[0]["reference_audio_base64"] == "REF"


async def test_en_cpu_se_recorta_a_500_en_fin_de_frase(red, monkeypatch):
    _device(monkeypatch, "cpu")
    texto = "Frase número uno del texto largo. " * 40
    r = await cv.CosyVoiceTool().speak(texto, "5215550001111")
    assert r.success and r.data["truncated"] and r.data["chars"] <= 500


async def test_falla_de_cosyvoice_no_lanza(monkeypatch):
    _device(monkeypatch, "cuda")
    monkeypatch.setattr(cv._req, "post", lambda *a, **k: SimpleNamespace(status_code=500, text="boom"))
    r = await cv.CosyVoiceTool().speak("Hola.", "5215550001111")
    assert not r.success and "500" in r.error
