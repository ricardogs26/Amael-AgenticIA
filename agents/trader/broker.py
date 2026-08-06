"""
broker.py — Wrapper de alpaca-py.

TRADER_MODE=paper → paper-api.alpaca.markets (dinero ficticio)
TRADER_MODE=live  → api.alpaca.markets (dinero real; NUNCA por default)

Credenciales: ALPACA_API_KEY / ALPACA_SECRET_KEY (Secret alpaca-credentials).
"""
from __future__ import annotations

import logging
import os

from agents.trader.models import AccountSnapshot

logger = logging.getLogger("agents.trader.broker")

TRADER_MODE = os.environ.get("TRADER_MODE", "paper").lower()

_trading_client = None


def is_paper() -> bool:
    return TRADER_MODE != "live"


def get_trading_client():
    """TradingClient singleton (paper por default)."""
    global _trading_client
    if _trading_client is None:
        from alpaca.trading.client import TradingClient
        _trading_client = TradingClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            paper=is_paper(),
        )
        logger.info(f"[broker] TradingClient inicializado (paper={is_paper()})")
    return _trading_client


def is_crypto(symbol: str) -> bool:
    return "/" in symbol  # convención Alpaca: BTC/USD, ETH/USD


def market_is_open() -> bool:
    """Reloj oficial del mercado (NYSE) según Alpaca."""
    try:
        return bool(get_trading_client().get_clock().is_open)
    except Exception as exc:
        logger.error(f"[broker] get_clock error: {exc}")
        return False


def get_account_snapshot() -> AccountSnapshot:
    client = get_trading_client()
    acct = client.get_account()
    positions = []
    for p in client.get_all_positions():
        positions.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value or 0),
            "unrealized_pl": float(p.unrealized_pl or 0),
            "avg_entry_price": float(p.avg_entry_price or 0),
        })
    return AccountSnapshot(
        equity_usd=float(acct.equity),
        cash_usd=float(acct.cash),
        positions=positions,
    )


def get_recent_bars(symbols: list[str], days: int = 14) -> dict[str, list[dict]]:
    """Barras diarias recientes por símbolo (contexto para el LLM)."""
    from datetime import datetime, timedelta, timezone

    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = datetime.now(timezone.utc) - timedelta(days=days + 3)
    out: dict[str, list[dict]] = {}

    stocks = [s for s in symbols if not is_crypto(s)]
    cryptos = [s for s in symbols if is_crypto(s)]

    def _pack(bars_obj):
        for sym, bars in bars_obj.data.items():
            out[sym] = [
                {"t": b.timestamp.strftime("%Y-%m-%d"), "o": round(b.open, 2),
                 "c": round(b.close, 2), "h": round(b.high, 2), "l": round(b.low, 2),
                 "v": int(b.volume)}
                for b in bars[-days:]
            ]

    try:
        if stocks:
            sc = StockHistoricalDataClient(
                api_key=os.environ["ALPACA_API_KEY"],
                secret_key=os.environ["ALPACA_SECRET_KEY"],
            )
            _pack(sc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=stocks, timeframe=TimeFrame.Day, start=start)))
        if cryptos:
            cc = CryptoHistoricalDataClient()  # datos cripto no requieren keys
            _pack(cc.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=cryptos, timeframe=TimeFrame.Day, start=start)))
    except Exception as exc:
        logger.error(f"[broker] get_recent_bars error: {exc}")
    return out


def get_intraday_bars(symbols: list[str], hours: int = 6) -> dict[str, list[dict]]:
    """Barras de 15 min de las últimas `hours` horas — señal intradía para el LLM."""
    from datetime import datetime, timedelta, timezone

    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    tf = TimeFrame(15, TimeFrameUnit.Minute)
    out: dict[str, list[dict]] = {}

    stocks = [s for s in symbols if not is_crypto(s)]
    cryptos = [s for s in symbols if is_crypto(s)]

    def _pack(bars_obj):
        for sym, bars in bars_obj.data.items():
            out[sym] = [
                {"t": b.timestamp.strftime("%H:%M"), "c": round(b.close, 2), "v": int(b.volume)}
                for b in bars
            ]

    try:
        if stocks:
            sc = StockHistoricalDataClient(
                api_key=os.environ["ALPACA_API_KEY"],
                secret_key=os.environ["ALPACA_SECRET_KEY"],
            )
            _pack(sc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=stocks, timeframe=tf, start=start)))
        if cryptos:
            cc = CryptoHistoricalDataClient()
            _pack(cc.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=cryptos, timeframe=tf, start=start)))
    except Exception as exc:
        logger.error(f"[broker] get_intraday_bars error: {exc}")
    return out


def submit_notional_order(symbol: str, side: str, notional_usd: float) -> dict:
    """
    Envía orden de mercado por monto (notional). Retorna dict con id/status.
    Usada para buys; los sells van por submit_sell_order (qty). Siempre tras el policy engine.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = get_trading_client()
    req = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional_usd, 2),
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        # Cripto exige GTC; equities con notional exigen DAY
        time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
    )
    order = client.submit_order(req)
    logger.info(f"[broker] Orden enviada: {side} {symbol} ${notional_usd:.2f} id={order.id}")
    return {"id": str(order.id), "status": str(order.status.value)}


def submit_sell_order(symbol: str, notional_usd: float, positions: list[dict]) -> dict:
    """
    Venta por qty de la posición real. Vender por notional falla cuando el precio
    subió desde la compra: Alpaca convierte el monto a qty al precio actual y puede
    exceder el balance disponible ("insufficient balance", code 40310000).
    - notional ≥ 95% del market_value → cierra la posición completa
    - venta parcial → orden por qty proporcional a la posición
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    plain = symbol.replace("/", "")
    pos = next((p for p in positions if p["symbol"] == plain), None)
    if pos is None or pos["qty"] <= 0:
        raise ValueError(f"sell sin posición abierta en {symbol}")
    if pos["market_value"] <= 0 or notional_usd >= pos["market_value"] * 0.95:
        return close_position(symbol)

    qty = round(pos["qty"] * notional_usd / pos["market_value"], 9)
    client = get_trading_client()
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
    )
    order = client.submit_order(req)
    logger.info(f"[broker] Orden enviada: sell {symbol} qty={qty} (~${notional_usd:.2f}) id={order.id}")
    return {"id": str(order.id), "status": str(order.status.value)}


def get_fees_summary(days: int = 7) -> dict:
    """
    Comisiones reales cobradas (activities CFEE/FEE de Alpaca), cacheadas 1h en
    Redis. La fricción real es el insumo del umbral de rentabilidad del LLM.
    """
    import json as _json
    import urllib.request
    from datetime import datetime, timedelta, timezone

    cache_key = f"trader:fees:{days}d"
    try:
        from storage.redis.client import get_client
        cached = get_client().get(cache_key)
        if cached:
            return _json.loads(cached)
    except Exception:
        pass

    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    base = "https://paper-api.alpaca.markets" if is_paper() else "https://api.alpaca.markets"
    url = f"{base}/v2/account/activities?activity_types=CFEE,FEE&after={after}&page_size=100"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    })
    # nosec B310 — el esquema es un literal https:// (constante `base`, sin
    # interpolación de input de usuario); B310 alerta por file:/ o esquemas
    # personalizados, imposibles aquí. Mismo patrón justificado que en
    # interfaces/api/routers/chat.py:792.
    acts = _json.load(urllib.request.urlopen(req, timeout=15))  # nosec B310
    total = sum(abs(float(a.get("net_amount") or 0)) for a in acts)
    out = {"days": days, "total_fees_usd": round(total, 2), "entries": len(acts)}
    try:
        from storage.redis.client import get_client
        get_client().set(cache_key, _json.dumps(out), ex=3600)
    except Exception:
        pass
    return out


def close_position(symbol: str) -> dict:
    """Cierra una posición completa (usado por sell sin notional explícito)."""
    client = get_trading_client()
    order = client.close_position(symbol.replace("/", ""))
    logger.info(f"[broker] Posición cerrada: {symbol} id={order.id}")
    return {"id": str(order.id), "status": str(order.status.value)}
