"""Pruebas de la transcripción de notas de voz (audio/transcriber.py)."""
import base64
from unittest.mock import MagicMock

import pytest

from audio import transcriber


class _Seg:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def modelo(monkeypatch):
    """Sustituye el modelo de whisper y captura los kwargs de transcribe()."""
    m = MagicMock()
    m.transcribe.return_value = ([_Seg("hola")], MagicMock(language="es"))
    monkeypatch.setattr(transcriber, "_get_model", lambda: m)
    return m


def _b64():
    return base64.b64encode(b"no importa: el modelo esta mockeado").decode()


def test_el_idioma_se_fija_por_defecto_en_espanol(modelo, monkeypatch):
    """Caso real (12-ago-2026): una nota de voz de 2.2 s en español se detectó
    como alemán con probabilidad 0.48 y se transcribió «Hola, como ist das bei
    uns die Sammeln?». Amael contestó «no entiendo la pregunta». En audios
    cortos la autodetección es una apuesta, y aquí el idioma se sabe."""
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")

    transcriber.transcribe_audio_base64(_b64())

    assert modelo.transcribe.call_args.kwargs["language"] == "es"


def test_auto_deja_que_whisper_detecte(modelo, monkeypatch):
    """Escape para audios en otro idioma: WHISPER_LANGUAGE=auto."""
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "auto")

    transcriber.transcribe_audio_base64(_b64())

    assert modelo.transcribe.call_args.kwargs["language"] is None


def test_vacio_tambien_significa_auto(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "")

    transcriber.transcribe_audio_base64(_b64())

    assert modelo.transcribe.call_args.kwargs["language"] is None


def test_devuelve_el_texto_transcripto(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")
    assert transcriber.transcribe_audio_base64(_b64()) == "hola"


def test_un_fallo_del_modelo_no_propaga(modelo, monkeypatch):
    """El llamador distingue '' (sin voz) de una excepción; romper aquí dejaría
    al usuario con el error genérico del bridge."""
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")
    modelo.transcribe.side_effect = RuntimeError("boom")

    assert transcriber.transcribe_audio_base64(_b64()) == ""
