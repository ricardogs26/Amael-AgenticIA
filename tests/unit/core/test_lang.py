"""Idioma de la conversación (core/lang.py)."""
from core import lang


def test_ingles_real_de_las_notas_de_voz():
    for t in ("Hi, Mael, how are you today?", "I'm fine and thank you. I have a question for you.",
              "Can you speak English?"):
        assert lang.detect(t) == "en", t


def test_espanol_real_de_las_notas_de_voz():
    for t in ("Hola, Mael, cómo estás? Muy buenos días.", "Hola, ¿cómo estás, Amael? Buenos días.",
              "¿Qué es eso de día hoy hoy plantas tus vegetales este domingo a las 8 a.m.?"):
        assert lang.detect(t) == "es", t


def test_el_contexto_en_espanol_no_tapa_una_pregunta_en_ingles():
    wrapped = ("Contexto del usuario (no lo cites ni menciones; úsalo solo si es relevante):\n"
               "- Prefiere respuestas cortas y directas\n\n[Pregunta actual]\nCan you speak English?")
    assert lang.effective(wrapped, "es") == "en"


def test_la_pregunta_manda_sobre_la_preferencia():
    assert lang.effective("How are you?", "es") == "en"
    assert lang.effective("¿Cómo estás?", "en") == "es"


def test_indeterminado_usa_la_preferencia_y_luego_espanol():
    assert lang.effective("OK", "en") == "en"
    assert lang.effective("OK", None) == "es"
    assert lang.effective("", "fr") == "es"


def test_instrucciones_para_los_dos_idiomas():
    assert set(lang.INSTRUCTION) == {"es", "en"}
