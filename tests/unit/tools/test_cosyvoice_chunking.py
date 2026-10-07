"""Textos largos para CosyVoice (tools/cosyvoice/chunking.py)."""
import io
import wave

from tools.cosyvoice import chunking as ch


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


def test_texto_corto_un_solo_fragmento():
    assert ch.split_for_tts("Hola Ricardo. ¿Cómo estás?") == ["Hola Ricardo. ¿Cómo estás?"]


def test_fragmentos_respetan_el_tope_y_no_pierden_texto():
    texto = " ".join(f"Esta es la frase número {i} del correo de prueba." for i in range(60))
    partes = ch.split_for_tts(texto, 450)
    assert all(len(p) <= 450 for p in partes)
    assert " ".join(partes).split() == texto.split()


def test_corta_en_fin_de_frase():
    texto = ("Primera frase completa. " * 30).strip()
    assert all(p.endswith(".") for p in ch.split_for_tts(texto, 100))


def test_frase_gigante_sin_puntos_se_parte_sin_romper_palabras():
    texto = "palabra " * 200
    partes = ch.split_for_tts(texto, 100)
    assert all(len(p) <= 100 for p in partes)
    assert all(" " not in w for p in partes for w in p.split())


def test_truncar_en_frase_nunca_a_media_palabra():
    texto = "Uno dos tres. Cuatro cinco seis siete ocho."
    assert ch.truncate_at_sentence(texto, 20) == "Uno dos tres."
    assert ch.truncate_at_sentence("abcdef ghijkl mnopq", 12) == "abcdef"


def test_tope_segun_dispositivo():
    assert ch.max_chars_for("cuda") == ch.MAX_CHARS_GPU > ch.max_chars_for("cpu") == ch.MAX_CHARS_CPU
    assert ch.max_chars_for(None) == ch.MAX_CHARS_CPU


def test_unir_wavs_suma_duracion_mas_pausas():
    out = ch.concat_wavs([_wav(1.0), _wav(2.0), _wav(0.5)], gap_ms=100)
    with wave.open(io.BytesIO(out), "rb") as w:
        dur = w.getnframes() / w.getframerate()
    assert abs(dur - 3.7) < 0.01


def test_formatos_distintos_no_se_mezclan():
    import pytest
    with pytest.raises(ValueError):
        ch.concat_wavs([_wav(1, 24000), _wav(1, 22050)])
