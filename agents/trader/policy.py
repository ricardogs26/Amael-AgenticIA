"""
policy.py — Policy engine determinista del trader.

Los límites viven AQUÍ (código + env vars), no en el prompt. El LLM no puede
negociarlos. Reglas evaluadas en orden; la primera que falla bloquea.

Env vars (ConfigMap trader-service-config):
  TRADER_MAX_ORDER_USD          default 20    — máx USD por orden
  TRADER_MAX_ORDERS_PER_DAY     default 5
  TRADER_HALT_EQUITY_USD        default 170   — equity < esto → HALT total
  TRADER_SYMBOL_WHITELIST       default "SPY,QQQ,VOO,BTC/USD,ETH/USD"
  TRADER_MIN_CONFIDENCE         default 0.6
  TRADER_CB_CONSECUTIVE_LOSSES  default 3     — pérdidas seguidas → pausa 24h
  TRADER_APPROVAL_THRESHOLD_USD default 50    — orden > esto → aprobación humana
  TRADER_EVENT_BLACKOUT_MIN     default 90    — min antes de evento macro sin buys
  TRADER_EVENT_BLACKOUT_AFTER_MIN default 30  — min después (ver macro_calendar)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from agents.trader.models import AccountSnapshot, PolicyDecision, TradeProposal

logger = logging.getLogger("agents.trader.policy")

MAX_ORDER_USD       = float(os.environ.get("TRADER_MAX_ORDER_USD", "20"))
MAX_ORDERS_PER_DAY  = int(os.environ.get("TRADER_MAX_ORDERS_PER_DAY", "5"))
# Tope de exposición acumulada por símbolo (evita piramidar todo el cupo diario
# en un solo activo). Default: 2× el máximo por orden.
MAX_SYMBOL_EXPOSURE_USD = float(os.environ.get("TRADER_MAX_SYMBOL_EXPOSURE_USD",
                                               str(MAX_ORDER_USD * 2)))
# Momentum mínimo (pct_6h) para buys. Cripto exige más: el round-trip en Alpaca
# cuesta ~0.5% (taker 0.25% x2 + spread); una señal menor no cubre la fricción.
MIN_MOMENTUM_PCT        = float(os.environ.get("TRADER_MIN_MOMENTUM_PCT", "0.7"))
MIN_MOMENTUM_PCT_CRYPTO = float(os.environ.get("TRADER_MIN_MOMENTUM_PCT_CRYPTO", "1.2"))
HALT_EQUITY_USD     = float(os.environ.get("TRADER_HALT_EQUITY_USD", "170"))
SYMBOL_WHITELIST    = [s.strip().upper() for s in os.environ.get(
    "TRADER_SYMBOL_WHITELIST", "SPY,QQQ,VOO,BTC/USD,ETH/USD").split(",") if s.strip()]
MIN_CONFIDENCE      = float(os.environ.get("TRADER_MIN_CONFIDENCE", "0.6"))
CB_CONSEC_LOSSES    = int(os.environ.get("TRADER_CB_CONSECUTIVE_LOSSES", "3"))
APPROVAL_USD        = float(os.environ.get("TRADER_APPROVAL_THRESHOLD_USD", "50"))

# Redis keys
KEY_HALT        = "trader:halt"               # flag persistente (sin TTL)
KEY_CB_PAUSE    = "trader:cb:pause"           # TTL 24h
KEY_CONSEC_LOSS = "trader:consecutive_losses"
KEY_ORDERS_DAY  = "trader:orders:{date}"      # contador diario, TTL 48h


def _redis():
    from storage.redis.client import get_client
    return get_client()


# ── Estado global (halt / circuit breaker) ────────────────────────────────────

def is_halted() -> bool:
    try:
        return _redis().exists(KEY_HALT) == 1
    except Exception:
        return False


def set_halt(reason: str) -> None:
    """HALT permanente — solo se quita a mano (DEL trader:halt o endpoint)."""
    _redis().set(KEY_HALT, f"{datetime.now(timezone.utc).isoformat()} {reason}")
    logger.critical(f"[policy] HALT activado: {reason}")


def clear_halt() -> None:
    _redis().delete(KEY_HALT)


def is_cb_paused() -> bool:
    try:
        return _redis().exists(KEY_CB_PAUSE) == 1
    except Exception:
        return False


def register_trade_result(pnl_usd: float) -> None:
    """Circuit breaker: N pérdidas realizadas consecutivas → pausa 24h."""
    r = _redis()
    if pnl_usd < 0:
        losses = r.incr(KEY_CONSEC_LOSS)
        if int(losses) >= CB_CONSEC_LOSSES:
            r.set(KEY_CB_PAUSE, "1", ex=86400)
            logger.warning(f"[policy] Circuit breaker: {losses} pérdidas seguidas → pausa 24h")
    else:
        r.delete(KEY_CONSEC_LOSS)


def orders_today() -> int:
    key = KEY_ORDERS_DAY.format(date=datetime.now(timezone.utc).strftime("%Y%m%d"))
    try:
        return int(_redis().get(key) or 0)
    except Exception:
        return 0


def count_order() -> None:
    key = KEY_ORDERS_DAY.format(date=datetime.now(timezone.utc).strftime("%Y%m%d"))
    r = _redis()
    r.incr(key)
    r.expire(key, 172800)


# ── Evaluación de propuestas ──────────────────────────────────────────────────

def evaluate(proposal: TradeProposal, account: AccountSnapshot,
             signals: dict | None = None) -> PolicyDecision:
    """Evalúa una propuesta del LLM contra todas las reglas. Determinista."""
    p = proposal

    if p.action == "hold":
        return PolicyDecision(approved=False, blocked_rule="hold", detail="LLM decidió no operar")

    if p.action not in ("buy", "sell"):
        return PolicyDecision(approved=False, blocked_rule="invalid_action", detail=f"action={p.action}")

    if is_halted():
        return PolicyDecision(approved=False, blocked_rule="halt", detail="HALT global activo")

    if is_cb_paused():
        return PolicyDecision(approved=False, blocked_rule="circuit_breaker", detail="pausa 24h por pérdidas consecutivas")

    if account.equity_usd < HALT_EQUITY_USD:
        set_halt(f"equity ${account.equity_usd:.2f} < ${HALT_EQUITY_USD:.2f}")
        return PolicyDecision(approved=False, blocked_rule="halt_equity", detail=f"equity ${account.equity_usd:.2f}")

    if p.symbol.upper() not in SYMBOL_WHITELIST:
        return PolicyDecision(approved=False, blocked_rule="symbol_whitelist", detail=f"{p.symbol} no está en whitelist")

    if p.confidence < MIN_CONFIDENCE:
        return PolicyDecision(approved=False, blocked_rule="min_confidence", detail=f"confidence {p.confidence:.2f} < {MIN_CONFIDENCE}")

    if orders_today() >= MAX_ORDERS_PER_DAY:
        return PolicyDecision(approved=False, blocked_rule="max_orders_per_day", detail=f"{orders_today()}/{MAX_ORDERS_PER_DAY}")

    if p.notional_usd <= 0:
        return PolicyDecision(approved=False, blocked_rule="invalid_notional", detail=f"${p.notional_usd}")

    if p.notional_usd > MAX_ORDER_USD:
        return PolicyDecision(approved=False, blocked_rule="max_order_usd",
                              detail=f"${p.notional_usd:.2f} > ${MAX_ORDER_USD:.2f}")

    # Blackout macro: nada de abrir posiciones alrededor de un CPI/NFP/PCE/FOMC.
    # Solo aplica a buys — vender siempre debe poder hacerse, si no el evento te
    # deja atrapado en la posición que querías cortar.
    if p.action == "buy":
        from agents.trader import macro_calendar
        try:
            ev = macro_calendar.blackout()
        except Exception as exc:
            # Fail-open: un calendario roto deja al trader tan ciego como antes,
            # pero no le impide operar ni tumba el ciclo.
            logger.error(f"[policy] calendario macro falló ({exc}) — sin blackout")
            ev = None
        if ev:
            return PolicyDecision(
                approved=False, blocked_rule="event_blackout",
                detail=f"{ev['evento']} {ev['fase']} ({abs(ev['minutos']):.0f} min)")

    if p.action == "buy" and signals:
        sig = signals.get(p.symbol) or signals.get(p.symbol.upper()) or {}
        pct6 = sig.get("pct_6h")
        vs_sma = sig.get("vs_sma14")
        from agents.trader.broker import is_crypto
        threshold = MIN_MOMENTUM_PCT_CRYPTO if is_crypto(p.symbol) else MIN_MOMENTUM_PCT
        # Excepción: estrategia de rebote (precio muy bajo el SMA14) no exige momentum 6h
        rebound = vs_sma is not None and vs_sma < -2
        if pct6 is not None and pct6 < threshold and not rebound:
            return PolicyDecision(approved=False, blocked_rule="min_momentum",
                                  detail=f"pct_6h {pct6:+.2f}% < {threshold}% "
                                         f"({'cripto' if is_crypto(p.symbol) else 'equity'})")

    if p.action == "buy":
        plain = p.symbol.upper().replace("/", "")
        current = sum(pos["market_value"] for pos in account.positions
                      if pos["symbol"].upper() == plain)
        if current + p.notional_usd > MAX_SYMBOL_EXPOSURE_USD:
            return PolicyDecision(approved=False, blocked_rule="max_symbol_exposure",
                                  detail=f"{p.symbol}: ${current:.2f} + ${p.notional_usd:.2f} "
                                         f"> ${MAX_SYMBOL_EXPOSURE_USD:.2f}")

    if p.action == "buy" and p.notional_usd > account.cash_usd:
        return PolicyDecision(approved=False, blocked_rule="insufficient_cash",
                              detail=f"cash ${account.cash_usd:.2f} < ${p.notional_usd:.2f}")

    if p.action == "sell" and not any(
            pos["symbol"].upper() in (p.symbol.upper(), p.symbol.upper().replace("/", ""))
            for pos in account.positions):
        return PolicyDecision(approved=False, blocked_rule="no_position", detail=f"sin posición en {p.symbol}")

    # En modo live, órdenes grandes requieren aprobación humana (WhatsApp)
    from agents.trader.broker import is_paper
    if not is_paper() and p.notional_usd > APPROVAL_USD:
        return PolicyDecision(approved=False, requires_approval=True,
                              detail=f"${p.notional_usd:.2f} > ${APPROVAL_USD:.2f} (live)")

    return PolicyDecision(approved=True, detail="ok")
