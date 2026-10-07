"""
Orden de motores de las respuestas en voz (_send_voice_note), desde 7-oct-2026.

CosyVoice pasó a la GPU (~1 s por segundo de audio, antes ~45). El orden viejo
—voz clonada → Piper → CosyVoice con la voz POR DEFECTO— se diseñó cuando
CosyVoice tardaba minutos, y su último recurso respondía a cualquier usuario
con la voz clonada de Ricardo. Ahora:
  - con voz propia y CosyVoice en GPU → voz clonada
  - sin voz propia y CosyVoice en GPU → CosyVoice con la voz neutral
  - Piper si lo anterior falla, o de entrada si CosyVoice cayó a CPU
  - nunca la voz por defecto (Ricardo) para otra persona
"""
from __future__ import annotations

import os

for _k, _v in {
    "INTERNAL_API_SECRET": "x" * 32, "JWT_SECRET_KEY": "x" * 32,
    "SESSION_SECRET_KEY": "x" * 32, "POSTGRES_PASSWORD": "t",
    "MINIO_ACCESS_KEY": "t", "MINIO_SECRET_KEY": "t",
}.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402

from core.tool_base import ToolOutput  # noqa: E402


@pytest.fixture
def engines(monkeypatch):
    """Sustituye voz de referencia, CosyVoice y Piper; registra qué se llamó."""
    import audio.voice_ref as vr
    import tools.cosyvoice.tool as cv
    import tools.piper.tool as pp

    state = {"ref": None, "device": "cuda", "fail": set(), "calls": []}

    monkeypatch.setattr(vr, "get_voice_reference", lambda phone: state["ref"])

    async def device(self):
        return state["device"]

    async def clone_send(self, inp):
        state["calls"].append("clone")
        return ToolOutput.fail("x", source="t") if "clone" in state["fail"] else \
            ToolOutput.ok(data={"duration_seconds": 1}, source="t")

    async def cosy_send(self, inp):
        state["calls"].append(f"cosy:{inp.voice}")
        return ToolOutput.fail("x", source="t") if "cosy" in state["fail"] else \
            ToolOutput.ok(data={"duration_seconds": 1}, source="t")

    async def piper_send(self, inp):
        state["calls"].append("piper")
        return ToolOutput.fail("x", source="t") if "piper" in state["fail"] else \
            ToolOutput.ok(data={"duration_seconds": 1}, source="t")

    monkeypatch.setattr(cv.CosyVoiceTool, "device", device)
    monkeypatch.setattr(cv.CosyVoiceTool, "synthesize_clone_and_send", clone_send)
    monkeypatch.setattr(cv.CosyVoiceTool, "synthesize_and_send", cosy_send)
    monkeypatch.setattr(pp.PiperTool, "synthesize_and_send", piper_send)
    return state


async def _send(text="Hola, mañana tienes cita a las diez."):
    from interfaces.api.routers.chat import _send_voice_note
    await _send_voice_note("5215550001111", text)


async def test_con_voz_propia_y_gpu_usa_la_clonada(engines):
    engines["ref"] = ("wavb64", "transcripción")
    await _send()
    assert engines["calls"] == ["clone"]


async def test_sin_voz_propia_usa_cosyvoice_neutral_no_piper(engines):
    await _send()
    assert engines["calls"] == ["cosy:es_MX_female"]


async def test_nunca_la_voz_por_defecto_para_otra_persona(engines):
    engines["fail"] = {"cosy", "piper"}
    await _send()
    assert all(c != "cosy:None" for c in engines["calls"])


async def test_clonada_falla_cae_a_neutral_y_luego_piper(engines):
    engines["ref"] = ("wavb64", "t")
    engines["fail"] = {"clone", "cosy"}
    await _send()
    assert engines["calls"] == ["clone", "cosy:es_MX_female", "piper"]


async def test_cosyvoice_en_cpu_va_directo_a_piper(engines):
    """En CPU una nota tardaría minutos: Piper responde en segundos."""
    engines["ref"] = ("wavb64", "t")
    engines["device"] = "cpu"
    await _send()
    assert engines["calls"] == ["piper"]


async def test_cosyvoice_en_cpu_y_piper_caido_ultimo_recurso_neutral(engines):
    engines["device"] = "cpu"
    engines["fail"] = {"piper"}
    await _send()
    assert engines["calls"] == ["piper", "cosy:es_MX_female"]


async def test_cosyvoice_inalcanzable_se_trata_como_cpu(engines):
    engines["device"] = None
    await _send()
    assert engines["calls"] == ["piper"]


async def test_texto_vacio_no_hace_nada(engines):
    await _send("   ")
    assert engines["calls"] == []
