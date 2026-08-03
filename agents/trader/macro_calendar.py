"""
macro_calendar.py — Calendario de eventos macro que mueven la whitelist.

La whitelist son ETFs de índice (SPY/QQQ/VOO) y cripto: no tienen earnings.
Lo que les pega son los datos macro de EE.UU. — CPI, empleo, PCE, GDP — y las
decisiones del FOMC. El loop de momentum es ciego a esto: puede comprar SPY 20
minutos antes de un CPI y comerse el gap completo.

Fuentes:
  · FRED API (api.stlouisfed.org) — fechas oficiales de publicación de los
    releases del BLS/BEA. Auto-actualizable, requiere FRED_API_KEY (gratis en
    https://fredaccount.stlouisfed.org/apikeys).
  · TRADER_FOMC_DATES — el FOMC no es un release de FRED; sus fechas se
    publican con 12 meses de anticipación y viven en el ConfigMap.

FRED entrega SOLO la fecha, no la hora. Las horas de publicación son fijas y
conocidas (8:30 ET para BLS/BEA, 14:00 ET para el comunicado del FOMC), así que
se mapean aquí por nombre de release.

Fail-open por diseño: si FRED no responde, la key falta o el caché está vacío,
`upcoming()` devuelve lo que haya (FOMC estático) y `blackout()` devuelve None.
Un calendario caído NO detiene al trader — solo lo deja tan ciego como antes.

Env vars:
  FRED_API_KEY                       — key de FRED (sin ella solo hay FOMC)
  TRADER_FOMC_DATES                  — CSV de fechas YYYY-MM-DD del FOMC
  TRADER_EVENT_BLACKOUT_MIN          default 90 — minutos ANTES del evento
  TRADER_EVENT_BLACKOUT_AFTER_MIN    default 30 — minutos DESPUÉS del evento
  TRADER_EVENT_LOOKAHEAD_HOURS       default 72 — horizonte que ve el LLM
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("agents.trader.macro_calendar")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred"

BLACKOUT_MIN = int(os.environ.get("TRADER_EVENT_BLACKOUT_MIN", "90"))
BLACKOUT_AFTER_MIN = int(os.environ.get("TRADER_EVENT_BLACKOUT_AFTER_MIN", "30"))
LOOKAHEAD_HOURS = int(os.environ.get("TRADER_EVENT_LOOKAHEAD_HOURS", "72"))

_ET = ZoneInfo("America/New_York")

# Releases de FRED que importan, por substring del nombre oficial.
#   (etiqueta corta, impacto, hora ET de publicación)
# El impacto "high" es el único que dispara blackout; "medium" solo informa.
TRACKED_RELEASES: dict[str, tuple[str, str, tuple[int, int]]] = {
    "consumer price index":         ("CPI", "high", (8, 30)),
    "employment situation":         ("NFP (empleo)", "high", (8, 30)),
    "personal income and outlays":  ("PCE", "high", (8, 30)),
    "gross domestic product":       ("GDP", "medium", (8, 30)),
}

FOMC_TIME_ET = (14, 0)   # comunicado; la conferencia de prensa es 14:30

# Redis
KEY_CACHE = "trader:calendar:events"
CACHE_TTL = 21600        # 6h — las fechas casi no cambian, pero no queremos
                         # arrastrar un error de un día entero

# Memo en proceso: red de seguridad si Redis está caído
_memo: tuple[datetime, list[dict]] | None = None


def _redis():
    from storage.redis.client import get_client
    return get_client()


def _floor(now: datetime) -> datetime:
    """
    Piso de la ventana: 1 día atrás, no `now`.

    La lista debe conservar los eventos recién pasados o la fase "después" del
    blackout depende de que el caché de Redis todavía los traiga — funcionaría
    a veces sí y a veces no.
    """
    return now - timedelta(days=1)


def _et_to_utc(date_str: str, hh: int, mm: int) -> datetime:
    """'2026-08-12' + 8:30 ET → datetime UTC (ZoneInfo resuelve el DST)."""
    y, m, d = (int(x) for x in date_str.split("-")[:3])
    return datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)


# ── FRED ──────────────────────────────────────────────────────────────────────

def _fred_get(path: str, **params) -> dict:
    import requests
    params.update({"api_key": FRED_API_KEY, "file_type": "json"})
    resp = requests.get(f"{FRED_BASE}/{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _fred_tracked_release_ids() -> dict[int, tuple[str, str, tuple[int, int]]]:
    """release_id → (etiqueta, impacto, hora ET) para los releases que seguimos.

    Se resuelve por nombre en vez de hardcodear IDs: los nombres son estables y
    un ID equivocado daría un calendario silenciosamente incorrecto.
    """
    data = _fred_get("releases", limit=1000)
    out: dict[int, tuple[str, str, tuple[int, int]]] = {}
    for rel in data.get("releases", []):
        name = str(rel.get("name", "")).lower()
        for needle, meta in TRACKED_RELEASES.items():
            if needle in name:
                out[int(rel["id"])] = meta
                break
    missing = {m[0] for m in TRACKED_RELEASES.values()} - {v[0] for v in out.values()}
    if missing:
        logger.warning(f"[calendar] releases de FRED no encontrados: {sorted(missing)}")
    return out


def _fetch_fred_events(now: datetime) -> list[dict]:
    """Publicaciones de los releases seguidos. Lanza si FRED falla."""
    ids = _fred_tracked_release_ids()
    if not ids:
        return []
    data = _fred_get(
        "releases/dates",
        realtime_start=_floor(now).date().isoformat(),
        realtime_end=(now + timedelta(days=45)).date().isoformat(),
        include_release_dates_with_no_data="true",
        sort_order="asc",
        limit=1000,
    )
    events: list[dict] = []
    for row in data.get("release_dates", []):
        meta = ids.get(int(row.get("release_id", -1)))
        if not meta:
            continue
        label, impact, (hh, mm) = meta
        when = _et_to_utc(str(row["date"]), hh, mm)
        if when < _floor(now):
            continue
        events.append({"name": label, "impact": impact, "when": when.isoformat(),
                       "source": "fred"})
    return events


# ── FOMC (estático) ───────────────────────────────────────────────────────────

def _fomc_events(now: datetime) -> list[dict]:
    raw = os.environ.get("TRADER_FOMC_DATES", "")
    events = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            when = _et_to_utc(chunk, *FOMC_TIME_ET)
        except (ValueError, IndexError):
            logger.warning(f"[calendar] fecha FOMC inválida ignorada: {chunk!r}")
            continue
        if when >= _floor(now):
            events.append({"name": "FOMC", "impact": "high", "when": when.isoformat(),
                           "source": "static"})
    return events


# ── API del módulo ────────────────────────────────────────────────────────────

def _load(now: datetime, force: bool = False) -> list[dict]:
    """Eventos futuros, cacheados. Nunca lanza — devuelve [] en el peor caso."""
    global _memo

    if not force:
        try:
            cached = _redis().get(KEY_CACHE)
            if cached:
                return json.loads(cached)
        except Exception as exc:
            logger.warning(f"[calendar] caché Redis no disponible: {exc}")
        if _memo and _memo[0] > now:
            return _memo[1]

    events = _fomc_events(now)
    if FRED_API_KEY:
        try:
            events += _fetch_fred_events(now)
        except Exception as exc:
            logger.error(f"[calendar] FRED no respondió ({exc}) — solo eventos estáticos")
    else:
        logger.warning("[calendar] FRED_API_KEY no configurada — solo eventos estáticos")

    events.sort(key=lambda e: e["when"])
    _memo = (now + timedelta(seconds=CACHE_TTL), events)
    try:
        _redis().set(KEY_CACHE, json.dumps(events), ex=CACHE_TTL)
    except Exception:
        pass
    if not events:
        logger.warning("[calendar] sin eventos futuros — el trader opera a ciegas")
    return events


def upcoming(hours: int | None = None, now: datetime | None = None) -> list[dict]:
    """Eventos dentro del horizonte, con horas restantes. Para el prompt del LLM."""
    now = now or datetime.now(timezone.utc)
    limit = now + timedelta(hours=hours if hours is not None else LOOKAHEAD_HOURS)
    out = []
    for ev in _load(now):
        when = datetime.fromisoformat(ev["when"])
        if now <= when <= limit:
            out.append({
                "evento": ev["name"],
                "impacto": ev["impact"],
                "en_horas": round((when - now).total_seconds() / 3600, 1),
                "utc": ev["when"],
            })
    return out


def blackout(now: datetime | None = None) -> dict | None:
    """
    Evento high-impact cuya ventana de blackout está activa ahora mismo, o None.

    Ventana = [evento - BLACKOUT_MIN, evento + BLACKOUT_AFTER_MIN]. El "después"
    importa tanto como el "antes": el minuto siguiente a un CPI es el de mayor
    spread y peor fill del día.
    """
    now = now or datetime.now(timezone.utc)
    for ev in _load(now):
        if ev["impact"] != "high":
            continue
        when = datetime.fromisoformat(ev["when"])
        if when - timedelta(minutes=BLACKOUT_MIN) <= now <= when + timedelta(minutes=BLACKOUT_AFTER_MIN):
            delta_min = (when - now).total_seconds() / 60
            return {
                "evento": ev["name"],
                "utc": ev["when"],
                "minutos": round(delta_min, 1),
                "fase": "antes" if delta_min >= 0 else "después",
            }
        if when > now + timedelta(minutes=BLACKOUT_MIN):
            break   # la lista está ordenada: lo que sigue está más lejos
    return None


def refresh() -> list[dict]:
    """Fuerza recarga desde FRED (endpoint de diagnóstico)."""
    return _load(datetime.now(timezone.utc), force=True)
