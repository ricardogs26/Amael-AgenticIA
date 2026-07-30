"""
thesis.py — Memoria de corto plazo del trader.

Dos piezas que hacen al agente coherente entre ciclos (fix de raíz del churn):

1. Trades recientes: resumen compacto de las últimas órdenes propias para el
   prompt — el LLM ve que acaba de salir de un símbolo y con qué resultado.
2. Tesis de trade: al comprar, el LLM declara su plan (target/stop/razón) y se
   persiste en Redis; mientras la posición viva, cada ciclo se le inyecta su
   propia tesis para que la salida sea coherente con la razón de entrada
   (una posición de rebote nace con pct_6h negativo — evaluarla con la
   heurística genérica de "sell defensivo" la mataba al siguiente ciclo).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("agents.trader.thesis")

THESIS_TTL_S = 7 * 24 * 3600  # una posición no debería vivir más de una semana


def _redis():
    from storage.redis.client import get_client
    return get_client()


def _key(symbol: str) -> str:
    return f"trader:thesis:{symbol.upper().replace('/', '')}"


def save_thesis(symbol: str, thesis: str, target_pct: float, stop_pct: float,
                entry_price: float | None = None) -> None:
    """Guarda el plan del LLM al abrir/agregar a una posición."""
    try:
        _redis().set(_key(symbol), json.dumps({
            "symbol": symbol.upper(),
            "thesis": thesis[:500],
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "entry_price": entry_price,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }), ex=THESIS_TTL_S)
    except Exception as exc:
        logger.warning(f"[thesis] no se pudo guardar tesis de {symbol}: {exc}")


def get_thesis(symbol: str) -> dict | None:
    try:
        raw = _redis().get(_key(symbol))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def clear_thesis(symbol: str) -> None:
    try:
        _redis().delete(_key(symbol))
    except Exception:
        pass


def open_theses(positions: list[dict]) -> list[dict]:
    """Tesis de las posiciones abiertas, enriquecidas con el P/L actual."""
    out = []
    for pos in positions:
        t = get_thesis(pos["symbol"]) or {"symbol": pos["symbol"],
                                          "thesis": "sin tesis registrada (posición previa)",
                                          "target_pct": None, "stop_pct": None}
        entry = pos.get("avg_entry_price") or 0
        mv, qty = pos.get("market_value") or 0, pos.get("qty") or 0
        price = mv / qty if qty else None
        pl_pct = round((price - entry) / entry * 100, 2) if price and entry else None
        t["pl_pct_vs_entry"] = pl_pct
        t["unrealized_pl_usd"] = pos.get("unrealized_pl")
        # Comparaciones precalculadas — los LLM chicos se equivocan comparando
        # números (leyó 0.8% como 8.0%); dárselas resueltas evita alucinaciones.
        if pl_pct is not None and t.get("target_pct") is not None:
            t["target_alcanzado"] = pl_pct >= t["target_pct"]
        if pl_pct is not None and t.get("stop_pct") is not None:
            t["stop_alcanzado"] = pl_pct <= t["stop_pct"]
        out.append(t)
    return out


def recent_trades_summary(limit: int = 10) -> list[dict]:
    """Últimas órdenes propias, compactas para el prompt."""
    from agents.trader.storage import get_recent_orders
    now = datetime.now(timezone.utc)
    out = []
    try:
        for o in get_recent_orders(limit):
            created = o.get("created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            mins = int((now - created).total_seconds() / 60) if created else None
            out.append({
                "hace_min": mins,
                "action": o.get("action"),
                "symbol": o.get("symbol"),
                "notional_usd": float(o.get("notional_usd") or 0),
                "status": o.get("status"),
                "blocked_rule": o.get("blocked_rule") or None,
                "reason": str(o.get("reason") or "")[:120],
            })
    except Exception as exc:
        logger.warning(f"[thesis] recent_trades_summary error: {exc}")
    return out
