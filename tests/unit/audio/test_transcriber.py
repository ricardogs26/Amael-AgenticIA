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
    m.detect_language.return_value = ("es", 0.9, [("es", 0.9), ("en", 0.05), ("de", 0.03)])
    monkeypatch.setattr(transcriber, "_get_model", lambda: m)
    monkeypatch.setattr(transcriber, "_decode", lambda path: "AUDIO")
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



def test_pista_de_vocabulario_con_el_nombre_amael(modelo, monkeypatch):
    """«Hola Amael» salía «Háblame el contacto» (7-oct-2026): la pista enseña el nombre."""
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")
    transcriber.transcribe_audio_base64(_b64())
    prompt = modelo.transcribe.call_args.kwargs["initial_prompt"]
    assert "Amael" in prompt
    assert "cómo estás" not in prompt.lower(), "pista de vocabulario, no un saludo que copiar"


def test_pista_vacia_se_apaga(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")
    monkeypatch.setattr(transcriber, "_WHISPER_PROMPT", "")
    transcriber.transcribe_audio_base64(_b64())
    assert modelo.transcribe.call_args.kwargs["initial_prompt"] is None


def test_modelo_por_defecto_es_small():
    import importlib
    import os
    if "WHISPER_MODEL" not in os.environ:
        assert importlib.reload(transcriber)._WHISPER_MODEL_SIZE == "small"



# ── es,en: detección restringida (7-oct-2026) ────────────────────────────────

def test_lista_elige_ingles_si_es_el_mas_probable(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es,en")
    modelo.detect_language.return_value = ("en", 0.59, [("en", 0.59), ("es", 0.25)])
    transcriber.transcribe_audio_base64(_b64())
    kw = modelo.transcribe.call_args.kwargs
    assert kw["language"] == "en"
    assert kw["initial_prompt"].startswith("Voice message for Amael")


def test_lista_ignora_idiomas_fuera_de_la_lista(modelo, monkeypatch):
    """El «hola» de agosto salió alemán con 0.48: con la lista gana el español."""
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es,en")
    modelo.detect_language.return_value = ("de", 0.48, [("de", 0.48), ("es", 0.30), ("en", 0.10)])
    transcriber.transcribe_audio_base64(_b64())
    assert modelo.transcribe.call_args.kwargs["language"] == "es"


def test_si_la_deteccion_falla_usa_el_primero_de_la_lista(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es,en")
    modelo.detect_language.side_effect = RuntimeError("boom")
    transcriber.transcribe_audio_base64(_b64())
    assert modelo.transcribe.call_args.kwargs["language"] == "es"


def test_idioma_fijo_no_detecta(modelo, monkeypatch):
    monkeypatch.setattr(transcriber, "_WHISPER_LANGUAGE", "es")
    transcriber.transcribe_audio_base64(_b64())
    modelo.detect_language.assert_not_called()
