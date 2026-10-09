"""Calificación de los ejercicios del english-coach (agents/english/grader.py)."""
import pytest

from agents.english import grader as g

REC = {
    "id": "EN-2026-10-08", "phrase": "circle back", "kind": "phrasal verb",
    "ex1": {"question": "Let's ____ to this after lunch.", "answer": "circle back"},
    "ex2": {"sentence": "I'll ____ with you tomorrow.", "correct": "B",
            "options": {"A": "wrap up", "B": "circle back", "C": "push back"}},
}


@pytest.mark.parametrize("texto,ex1,ex2", [
    ("1) circle back 2) B", "circle back", "B"),
    ("1. circle back\n2. b", "circle back", "B"),
    ("circle back, B", "circle back", "B"),
    ("B", None, "B"),
    ("circle back", "circle back", None),
    ("2) wrap up", None, "wrap up"),
])
def test_interpreta_respuestas_naturales(texto, ex1, ex2):
    r = g.parse_reply(texto)
    assert (r.ex1, r.ex2, r.reveal) == (ex1, ex2, False)


@pytest.mark.parametrize("texto", ["answer", "Answers", "respuesta", "¿respuestas?", "solution"])
def test_pedir_las_soluciones(texto):
    assert g.parse_reply(texto).reveal


@pytest.mark.parametrize("resp", ["circle back", "Circle back!", "circled back", "circling back",
                                  "Let's circle back to this after lunch."])
def test_ejercicio1_acepta_formas_y_oracion_completa(resp):
    assert g.check_ex1(resp, REC)


@pytest.mark.parametrize("resp", ["come back", "circle", "back", "follow up"])
def test_ejercicio1_rechaza_lo_incorrecto(resp):
    assert not g.check_ex1(resp, REC)


def test_ejercicio2_por_letra_o_por_texto():
    assert g.check_ex2("B", REC) and g.check_ex2("circle back", REC)
    assert not g.check_ex2("A", REC) and not g.check_ex2("wrap up", REC)


def test_retro_ambos_bien():
    msg = g.feedback(g.parse_reply("1) circle back 2) B"), REC)
    assert msg.count("✅") == 2 and "2/2" in msg


def test_retro_con_errores_dice_lo_correcto():
    msg = g.feedback(g.parse_reply("1) come back 2) A"), REC)
    assert msg.count("❌") == 2
    assert "come back" in msg and "circle back" in msg and "B) circle back" in msg and "0/2" in msg


def test_retro_parcial_pide_lo_que_falta():
    msg = g.feedback(g.parse_reply("B"), REC)
    assert "✅" in msg and "Exercise 1" in msg and "pending" in msg.lower()


def test_soluciones():
    msg = g.feedback(g.parse_reply("answer"), REC)
    assert "circle back" in msg and "B)" in msg and "✅" not in msg


def test_id_de_la_leccion_en_la_cita():
    assert g.lesson_id("…blah\n🆔 EN-2026-10-08") == "EN-2026-10-08"
    assert g.lesson_id("sin id") is None


# ── Respuestas por nota de voz (Whisper en inglés) ───────────────────────────

@pytest.mark.parametrize("dicho,ex1,ex2", [
    ("Number one, circle back. Number two, B.", "circle back", "B"),
    ("One: circle back. Two: bee.", "circle back", "B"),
    ("First, circle back. Second, letter C.", "circle back", "C"),
    ("Circle back and option B.", "circle back", "B"),
    ("Bee.", None, "B"),
    ("See", None, "C"),
    ("Circle back.", "circle back", None),
    ("number two, A", None, "A"),
])
def test_lo_hablado_se_convierte_al_formato_escrito(dicho, ex1, ex2):
    r = g.parse_reply(g.spoken_to_text(dicho))
    assert (r.ex1 and g._fold(r.ex1), r.ex2) == (ex1, ex2)


def test_lo_hablado_no_rompe_frases_con_one():
    """«someone» / «one's» no son el número uno."""
    r = g.parse_reply(g.spoken_to_text("Can I pick someone's brain? Number two, A."))
    assert r.ex2 == "A" and "someone" in r.ex1


def test_retro_de_voz_dice_lo_que_se_escucho():
    msg = g.feedback(g.parse_reply(g.spoken_to_text("Number one, circle back. Number two, bee.")), REC,
                     heard="Number one, circle back. Number two, bee.")
    assert msg.startswith("🎧 I heard:") and "2/2" in msg
