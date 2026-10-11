"""
Crear un evento de calendario desde el chat («agéndalo», «agenda la cita del
dentista el sábado a la 1:30»).

Reparto de responsabilidades — el mismo de Cassiel:
  - el LLM SOLO extrae campos del texto (título, fecha, hora, lugar, notas) a
    JSON; no decide, no calcula, no confirma;
  - el CÓDIGO valida (fecha parseable, hora presente, futuro, zona horaria),
    rellena defaults (60 min, título «Cita») y redacta la confirmación;
  - lo que el código no puede resolver (sin hora, sin fecha) se le pregunta
    al usuario UNA vez, con un mensaje fijo — nunca una re-pregunta del LLM.

Caso real 10-oct-2026: el recordatorio de Dentalitos (sáb 24-oct 13:30,
Regina, Dra. Luz María) — Amael no tenía con qué agendarlo.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("agents.productivity.event_extractor")

_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL           = os.environ.get("LLM_MODEL", "qwen3.5:9b")
_LLM_TIMEOUT_S   = int(os.environ.get("EVENT_EXTRACT_LLM_TIMEOUT_S", "60"))

DEFAULT_DURATION_MIN = 60
DEFAULT_TITLE        = "Cita"
_MAX_TEXT_CHARS      = 3000

_SYSTEM = """Eres Haniel, el asistente de calendario de Amael. Extrae del texto los datos de UN evento y devuelve SOLO un objeto JSON, sin texto adicional.

Esquema:
{
  "title": "título corto y útil (ej. «Cita dental — Regina (Dra. Luz María)»)",
  "date": "YYYY-MM-DD" | "hoy" | "mañana" | "pasado mañana" | día de la semana ("jueves") | null,
  "time": "HH:MM" en 24 h | null,
  "duration_min": entero | null,
  "location": "dirección o enlace de mapa" | "",
  "notes": "teléfonos, paciente, médico, instrucciones — lo que convenga tener a la mano" | ""
}

Reglas:
- Copia los datos del texto; NO inventes fecha ni hora. Si no aparecen, pon null.
- Fecha relativa («el jueves», «mañana», «pasado mañana», «hoy») → escríbela tal cual en "date"; el código la convierte.
- Si el texto da el día del mes sin año («sábado 24 octubre»), usa el año en curso que se te indica; el código corrige si ya pasó.
- «01:30 PM» → "13:30". «9 am» → "09:00". «a la 1» de una cita médica → "13:00".
- El usuario puede dar una orden breve al inicio y el texto reenviado después: los datos del evento están en el texto reenviado.
- Responde SOLO el JSON."""

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

_YEAR_RE = re.compile(r"\b20\d\d\b")


class EventoIncompleto(ValueError):
    """Falta un dato que el código no puede suponer. `faltan` lista qué."""

    def __init__(self, faltan: list[str]):
        self.faltan = faltan
        super().__init__("faltan: " + ", ".join(faltan))


@dataclass
class EventSpec:
    title: str
    start: datetime          # con tzinfo
    end: datetime            # con tzinfo
    location: str = ""
    notes: str = ""
    adjusted_year: bool = False
    defaults_used: list[str] = field(default_factory=list)

    @property
    def start_iso(self) -> str:
        return self.start.replace(tzinfo=None).isoformat(timespec="seconds")

    @property
    def end_iso(self) -> str:
        return self.end.replace(tzinfo=None).isoformat(timespec="seconds")


# ── LLM: texto → JSON crudo ─────────────────────────────────────────────────

def extract_event_fields(text: str, now: datetime) -> dict:
    """Llama al LLM en modo JSON. Bloqueante — el agente lo manda a un hilo."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    llm = ChatOllama(
        model=_MODEL,
        base_url=_OLLAMA_BASE_URL,
        temperature=0.0,
        reasoning=False,
        format="json",
        client_kwargs={"timeout": _LLM_TIMEOUT_S},
    )
    contexto = (
        f"Hoy es {_DIAS[now.weekday()]} {now.strftime('%Y-%m-%d %H:%M')} "
        f"({now.tzinfo}). Año en curso: {now.year}.\n\n"
        f"Texto:\n{text[:_MAX_TEXT_CHARS]}"
    )
    resp = llm.invoke([SystemMessage(content=_SYSTEM), HumanMessage(content=contexto)])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"El LLM no devolvió un objeto: {type(data)}")
    return data


# ── Código: JSON → EventSpec validado ────────────────────────────────────────

_RELATIVOS = {"hoy": 0, "mañana": 1, "manana": 1, "pasado mañana": 2, "pasado manana": 2}
_DIAS_SIN_ACENTO = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _sin_acentos(s: str) -> str:
    return (s.lower().replace("á", "a").replace("é", "e").replace("í", "i")
            .replace("ó", "o").replace("ú", "u"))


def _parse_date(value, today: date | None = None) -> date | None:
    """ISO, o relativo resuelto en código: «hoy», «mañana», «pasado mañana»,
    «jueves» (= el PRÓXIMO jueves; si hoy es jueves, hoy)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    if today is None:
        return None
    key = _sin_acentos(re.sub(r"^(el|este|próximo|proximo)\s+", "", v.lower())).strip()
    if key in _RELATIVOS:
        return today + timedelta(days=_RELATIVOS[key])
    if key in _DIAS_SIN_ACENTO:
        delta = (_DIAS_SIN_ACENTO.index(key) - today.weekday()) % 7
        return today + timedelta(days=delta)
    return None


def _parse_time(value) -> tuple[int, int] | None:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"^\s*(\d{1,2})[:.h](\d{2})\s*$", value)
    if not m:
        m = re.match(r"^\s*(\d{1,2})\s*$", value)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    else:
        h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def _parse_duration(value) -> int | None:
    try:
        d = int(value)
    except (TypeError, ValueError):
        return None
    return d if 5 <= d <= 24 * 60 else None


def resolve_event(parsed: dict, text: str, now: datetime) -> EventSpec:
    """Valida el JSON del LLM contra reglas fijas. Lanza EventoIncompleto si
    falta fecha u hora. `now` debe traer tzinfo (zona del usuario)."""
    tz = now.tzinfo or ZoneInfo("America/Mexico_City")
    faltan: list[str] = []

    d = _parse_date(parsed.get("date"), now.date())
    if d is None:
        faltan.append("fecha")
    t = _parse_time(parsed.get("time"))
    if t is None:
        faltan.append("hora")
    if faltan:
        raise EventoIncompleto(faltan)

    adjusted_year = False
    # Fecha ya pasada y el texto no trae año explícito → el remitente daba el
    # día del mes («sábado 24 octubre») y el LLM puso el año en curso cuando
    # ya era del siguiente. Con año explícito en el texto se respeta tal cual.
    if d < now.date() and not _YEAR_RE.search(text):
        d = d.replace(year=d.year + 1)
        adjusted_year = True

    defaults: list[str] = []
    duration = _parse_duration(parsed.get("duration_min"))
    if duration is None:
        duration = DEFAULT_DURATION_MIN
        defaults.append("duración")

    title = str(parsed.get("title") or "").strip()[:120]
    if not title:
        title = DEFAULT_TITLE
        defaults.append("título")

    start = datetime(d.year, d.month, d.day, t[0], t[1], tzinfo=tz)
    end = start + timedelta(minutes=duration)
    return EventSpec(
        title=title,
        start=start,
        end=end,
        location=str(parsed.get("location") or "").strip()[:500],
        notes=str(parsed.get("notes") or "").strip()[:2000],
        adjusted_year=adjusted_year,
        defaults_used=defaults,
    )


# ── Código: EventSpec → mensajes al usuario ──────────────────────────────────

def _fecha_humana(dt: datetime) -> str:
    return f"{_DIAS[dt.weekday()][:3]} {dt.day:02d}-{_MESES[dt.month - 1]}-{dt.year}"


def format_confirmation(spec: EventSpec, link: str = "") -> str:
    """Respuesta fija — es lo que Ricardo pidió: que diga QUÉ quedó agendado."""
    lineas = [
        f"📅 Agendado: {_fecha_humana(spec.start)}, "
        f"{spec.start.strftime('%H:%M')}–{spec.end.strftime('%H:%M')}",
        spec.title,
    ]
    if spec.location:
        lineas.append(f"📍 {spec.location}")
    if spec.notes:
        lineas.append(f"📝 {spec.notes}")
    if spec.adjusted_year:
        lineas.append(f"(la fecha ya había pasado este año; lo puse en {spec.start.year})")
    if link:
        lineas.append(link)
    return "\n".join(lineas)


def format_incomplete(faltan: list[str]) -> str:
    que = " y ".join(faltan)
    return (
        f"Para agendarlo me falta la {que} 🗓 "
        f"Mándame de nuevo la orden con ese dato, por ejemplo: "
        f"«agéndalo el sábado 24 de octubre a la 1:30 pm»."
    )
