"""
Calificación de los ejercicios del english-coach (8-oct-2026).

Ricardo responde CITANDO el mensaje de la lección; el bridge manda la cita y la
respuesta a /api/english/answer. Todo es determinista: la lección (frase,
respuestas, opción correcta) la dejó el job en Redis; aquí no hay LLM.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TAG_RE = re.compile(r"\bEN-\d{4}-\d{2}-\d{2}\b")
_REVEAL = {"answer", "answers", "respuesta", "respuestas", "solution", "solutions",
           "solucion", "soluciones"}
_PLACEHOLDERS = {"someone", "someone's", "somebody", "something", "sb", "sth", "one's", "oneself"}
_NUMBERED = re.compile(r"(?:^|\s|,)([12])\s*[).:\-]\s*")


@dataclass
class Reply:
    ex1: str | None = None
    ex2: str | None = None
    reveal: bool = False


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", s)).strip()


def lesson_id(quoted: str) -> str | None:
    m = _TAG_RE.search(quoted or "")
    return m.group(0) if m else None


def parse_reply(text: str) -> Reply:
    """«1) circle back 2) B», «circle back, B», «B», «circle back», «answer»."""
    raw = (text or "").strip()
    if _fold(raw) in _REVEAL:
        return Reply(reveal=True)
    marks = list(_NUMBERED.finditer(raw))
    if marks:
        out = Reply()
        prefix = raw[: marks[0].start()].strip(" ,;\n")
        if prefix and marks[0].group(1) == "2":
            out.ex1 = prefix         # «look after, 2) A» / voz: «… Number two, A»
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            val = raw[m.end():end].strip(" ,;\n")
            if not val:
                continue
            if m.group(1) == "1":
                out.ex1 = val
            else:
                out.ex2 = val.upper() if re.fullmatch(r"[A-Ca-c]", val) else val
        return out
    letter = re.search(r"(?:^|[\s,;])([A-Ca-c])[.)!]?\s*$", raw)
    if letter:
        rest = raw[: letter.start(1)].strip(" ,;")
        return Reply(ex1=rest or None, ex2=letter.group(1).upper())
    return Reply(ex1=raw or None)


# ── Respuestas por nota de voz (8-oct-2026) ──────────────────────────────────
_LETTER_WORDS = {"a": "A", "ay": "A", "eh": "A", "b": "B", "be": "B", "bee": "B",
                 "c": "C", "see": "C", "sea": "C", "si": "C"}
_SPOKEN_NUM = re.compile(
    r"(?:^|(?<=[.,;!?])\s*|\band\s+)(?:number\s+)?(one|first|two|second)\b\s*[:,.\-]?\s*",
    re.IGNORECASE,
)


def _letter(segment: str) -> str | None:
    f = re.sub(r"^(?:option|letter|the)\s+", "", _fold(segment)).strip()
    return _LETTER_WORDS.get(f)


def spoken_to_text(transcript: str) -> str:
    """Whisper → formato escrito: «Number one, look after. Number two, bee.» →
    «1) look after. 2) B». Los números solo cuentan al inicio, tras puntuación
    o tras «and» («someone»/«one's» no son el uno); las letras habladas
    (bee/see/ay, «option B», «letter C») se vuelven A/B/C."""
    t = (transcript or "").strip()
    whole = _letter(t)
    if whole:
        return whole
    t = _SPOKEN_NUM.sub(
        lambda m: f" {'1' if m.group(1).lower() in ('one', 'first') else '2'}) ", t
    ).strip()
    if "2)" in t:
        head, tail = t.rsplit("2)", 1)
        letter = _letter(tail)
        return f"{head}2) {letter}" if letter else t
    m = re.search(r"[,\s]+(?:and\s+)?(?:option|letter)\s+([abcABC])\W*$", t)
    if m:
        return f"{t[: m.start()].strip()}, {m.group(1).upper()}"
    m = re.search(r"[,\s]+(?:and\s+)?(bee|be|see|sea|si)\W*$", t, re.IGNORECASE)
    if m:
        return f"{t[: m.start()].strip()}, {_LETTER_WORDS[m.group(1).lower()]}"
    return t


def check_ex1(answer: str, rec: dict) -> bool:
    """Correcto si trae la respuesta esperada, o la frase con sus conjugaciones
    (raíz de 4 letras al inicio de palabra; las cortas, completas)."""
    a = _fold(answer)
    if not a:
        return False
    expected = _fold(rec["ex1"]["answer"])
    if expected and (a == expected or expected in a):
        return True
    toks = a.split()
    words = [w for w in _fold(rec["phrase"]).split() if w not in _PLACEHOLDERS]
    if not words:
        return False
    for w in words:
        if len(w) >= 4:
            if not any(t.startswith(w[:4]) for t in toks):
                return False
        elif w not in toks:
            return False
    return True


def check_ex2(answer: str, rec: dict) -> bool:
    a = (answer or "").strip()
    ex2 = rec["ex2"]
    if re.fullmatch(r"[A-Ca-c]", a):
        return a.upper() == ex2["correct"]
    return _fold(a) == _fold(ex2["options"][ex2["correct"]])


def feedback(reply: Reply, rec: dict, heard: str | None = None) -> str:
    """`heard`: lo que Whisper entendió de una nota de voz. Va arriba: si
    escuchó otra cosa, es una pista de pronunciación."""
    body = _feedback(reply, rec)
    return f"🎧 I heard: «{heard.strip()}»\n\n{body}" if heard else body


def _feedback(reply: Reply, rec: dict) -> str:
    ex2 = rec["ex2"]
    correct2 = f"{ex2['correct']}) {ex2['options'][ex2['correct']]}"
    if reply.reveal:
        return (f"📘 *Solutions* — {rec['phrase']}\n"
                f"1) {rec['ex1']['answer']}\n2) {correct2}")
    lines, score, done = [], 0, 0
    if reply.ex1 is not None:
        done += 1
        if check_ex1(reply.ex1, rec):
            score += 1
            lines.append(f"✅ Exercise 1: correct — *{rec['ex1']['answer']}*")
        else:
            lines.append(f"❌ Exercise 1: you wrote «{reply.ex1}». Answer: *{rec['ex1']['answer']}*")
    if reply.ex2 is not None:
        done += 1
        chosen = reply.ex2
        if re.fullmatch(r"[A-C]", chosen):
            chosen = f"{chosen}) {ex2['options'].get(chosen, '?')}"
        if check_ex2(reply.ex2, rec):
            score += 1
            lines.append(f"✅ Exercise 2: correct — {correct2}")
        else:
            lines.append(f"❌ Exercise 2: you chose {chosen}. Correct: {correct2}")
    if done == 0:
        return "I couldn't read your answers. Reply like: _1) your answer 2) A_ — or _answer_ for the solutions."
    if done == 2:
        lines.append(f"\nScore: {score}/2 {'🎉' if score == 2 else '💪'}")
    else:
        missing = 2 if reply.ex2 is None else 1
        lines.append(f"\nExercise {missing} still pending — reply quoting the lesson again.")
    return "\n".join(lines)
