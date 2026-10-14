"""
Brief del día armado por CÓDIGO, por secciones fijas (Fase 2 C, 14-oct-2026):

  📅 Hoy             eventos del calendario (código)
  🔜 Próximos días   eventos de los próximos 7 días (código, entrega D)
  ⏰ Vence esta semana pendientes/compromisos con fecha ≤ 7 días (código)
  📝 Pendientes      top-3 de Cassiel + análisis nocturno (código + nocturno)
  🧭 Plan del día    resumen, bloques y prioridades — lo ÚNICO que escribe el
                     LLM, y los bloques pasan por `validate_plan_tasks`
  ⚠️ Alertas         choques del LLM + avisos del sistema (código)

Antes el LLM recibía 10 asuntos recortados y en una sola pasada juzgaba
urgencia, repartía bloques y redactaba todo. Ahora el código reúne y ordena
los hechos; el LLM solo reparte el día alrededor de ellos. Si el LLM falla,
el brief sale igual sin la sección 🧭.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

WORK_START = os.environ.get("BRIEF_WORK_START", "08:00")
WORK_END   = os.environ.get("BRIEF_WORK_END", "18:00")
MAX_UPCOMING = 8
MAX_DUE      = 6

_DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_HHMM = re.compile(r"^(\d{2}):(\d{2})$")


# ── Utilidades de hora ───────────────────────────────────────────────────────

def _min(hhmm: str) -> int | None:
    m = _HHMM.match(str(hhmm or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _hora_de(evento: dict) -> str:
    inicio = str(evento.get("start", ""))
    return inicio.split("T", 1)[1][:5] if "T" in inicio else ""


def _fin_de(evento: dict) -> str:
    fin = str(evento.get("end", ""))
    return fin.split("T", 1)[1][:5] if "T" in fin else ""


def _fecha_de(evento: dict) -> date | None:
    try:
        return date.fromisoformat(str(evento.get("start", ""))[:10])
    except ValueError:
        return None


def _fecha_humana(d: date, today: date) -> str:
    if d == today + timedelta(days=1):
        return "mañana"
    return f"{_DIAS[d.weekday()]} {d.day:02d}-{_MESES[d.month - 1]}"


def _monto(amount) -> str:
    if amount is None:
        return ""
    a = float(amount)
    return f" · ${a:,.0f}" if a.is_integer() else f" · ${a:,.2f}"


# ── Validación del plan del LLM ──────────────────────────────────────────────

def validate_plan_tasks(plan_tasks: list, events_today: list) -> tuple[list[dict], list[str]]:
    """Filtra los bloques del LLM: hora válida, inicio < fin, dentro del
    horario laboral, sin encimarse con eventos reales ni entre sí. Devuelve
    (bloques válidos, motivos de descarte)."""
    ini_lab, fin_lab = _min(WORK_START), _min(WORK_END)
    ocupados: list[tuple[int, int, str]] = []
    for ev in events_today or []:
        a, b = _min(_hora_de(ev)), _min(_fin_de(ev))
        if a is not None and b is not None and a < b:
            ocupados.append((a, b, str(ev.get("summary", "evento"))))

    ok: list[dict] = []
    descartados: list[str] = []
    for t in plan_tasks or []:
        if not isinstance(t, dict):
            continue
        titulo = str(t.get("title") or "(bloque)")
        a, b = _min(t.get("start")), _min(t.get("end"))
        if a is None or b is None or a >= b:
            descartados.append(f"{titulo}: hora inválida ({t.get('start')}–{t.get('end')})")
            continue
        if a < ini_lab or b > fin_lab:
            descartados.append(f"{titulo}: fuera del horario {WORK_START}–{WORK_END}")
            continue
        choque = next((n for (x, y, n) in ocupados if a < y and b > x), None)
        if choque:
            descartados.append(f"{titulo}: se encima con «{choque}»")
            continue
        ok.append({**t, "title": titulo, "start": t["start"], "end": t["end"]})
        ocupados.append((a, b, titulo))
    return ok, descartados


# ── Secciones ────────────────────────────────────────────────────────────────

def section_today(events_today: list) -> str:
    lineas = ["📅 *Hoy*"]
    if not events_today:
        lineas.append("Sin eventos en el calendario.")
        return "\n".join(lineas)
    con_hora = sorted((e for e in events_today if _hora_de(e)), key=_hora_de)
    todo_el_dia = [e for e in events_today if not _hora_de(e)]
    for e in todo_el_dia:
        lineas.append(f"• todo el día · {e.get('summary', '(sin título)')}")
    for e in con_hora:
        fin = _fin_de(e)
        lineas.append(f"• {_hora_de(e)}{'–' + fin if fin else ''} {e.get('summary', '(sin título)')}"
                      + (f" · 📍 {e['location']}" if e.get("location") else ""))
    return "\n".join(lineas)


def section_upcoming(events: list, today: date) -> str:
    futuros = [(f, e) for e in events or [] if (f := _fecha_de(e)) and f > today]
    if not futuros:
        return ""
    futuros.sort(key=lambda fe: (fe[0], _hora_de(fe[1])))
    lineas = ["🔜 *Próximos días*"]
    for f, e in futuros[:MAX_UPCOMING]:
        hora = _hora_de(e)
        lineas.append(f"• {_fecha_humana(f, today)}{' ' + hora if hora else ''} · "
                      f"{e.get('summary', '(sin título)')}")
    if len(futuros) > MAX_UPCOMING:
        lineas.append(f"(y {len(futuros) - MAX_UPCOMING} más)")
    return "\n".join(lineas)


def section_due_week(tasks: list, today: date) -> str:
    limite = today + timedelta(days=7)
    vencen = [t for t in tasks or []
              if t.status == "pending" and t.due_date is not None and t.due_date <= limite]
    if not vencen:
        return ""
    vencen.sort(key=lambda t: (t.due_date, t.id))
    lineas = ["⏰ *Vence esta semana*"]
    for t in vencen[:MAX_DUE]:
        d = (t.due_date - today).days
        cuando = (f"{-d} día{'s' if d != -1 else ''} de atraso" if d < 0 else
                  "hoy" if d == 0 else "mañana" if d == 1 else
                  f"{_DIAS[t.due_date.weekday()]} {t.due_date.day:02d}")
        origen = " (del correo)" if getattr(t, "source", "chat") == "email" else ""
        lineas.append(f"• {cuando} · #{t.id} {t.title}{_monto(t.amount)}{origen}")
    if len(vencen) > MAX_DUE:
        lineas.append(f"(y {len(vencen) - MAX_DUE} más)")
    return "\n".join(lineas)


def section_plan(plan: dict, bloques: list[dict]) -> str:
    if not plan:
        return ""
    lineas = ["🧭 *Plan del día*"]
    resumen = str(plan.get("summary") or "").strip()
    if resumen:
        lineas.append(resumen)
    for b in bloques:
        lineas.append(f"• {b['start']}–{b['end']} {b['title']}")
    prioridades = [str(p) for p in (plan.get("priorities") or []) if str(p).strip()]
    if prioridades:
        lineas.append("Prioridades:")
        lineas += [f"• {p}" for p in prioridades[:3]]
    return "\n".join(lineas)


def section_alerts(llm_warnings: list, system_alerts: list) -> str:
    items = [str(a) for a in (system_alerts or []) if str(a).strip()]
    items += [str(w) for w in (llm_warnings or []) if str(w).strip()]
    if not items:
        return ""
    return "\n".join(["⚠️ *Alertas*"] + [f"• {i}" for i in items])


def assemble(*, today: date, section_today_txt: str, upcoming_txt: str, due_txt: str,
             pending_txt: str, plan_txt: str, alerts_txt: str) -> str:
    partes = [section_today_txt, upcoming_txt, due_txt, pending_txt, plan_txt, alerts_txt]
    return "\n\n".join(p.strip() for p in partes if p and p.strip())
