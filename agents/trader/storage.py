"""
storage.py — Persistencia del trader en PostgreSQL.

Tablas:
  trader_orders  — cada decisión (ejecutada, bloqueada o pendiente) con la razón del LLM
  trader_equity  — snapshot de equity/cash por ciclo (curva de capital)
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("agents.trader.storage")

_DDL = """
CREATE TABLE IF NOT EXISTS trader_orders (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode            TEXT NOT NULL,              -- paper | live
    action          TEXT NOT NULL,              -- buy | sell | hold
    symbol          TEXT NOT NULL DEFAULT '',
    notional_usd    NUMERIC(12,2) NOT NULL DEFAULT 0,
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',   -- justificación del LLM
    status          TEXT NOT NULL,              -- executed | blocked | pending_approval | error
    blocked_rule    TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '',
    alpaca_order_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trader_orders_created ON trader_orders (created_at DESC);

CREATE TABLE IF NOT EXISTS trader_equity (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode        TEXT NOT NULL,
    equity_usd  NUMERIC(12,2) NOT NULL,
    cash_usd    NUMERIC(12,2) NOT NULL,
    positions   JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_trader_equity_created ON trader_equity (created_at DESC);
"""


def init_trader_db() -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    logger.info("[storage] Schema trader_orders/trader_equity verificado")


def store_order(
    mode: str, action: str, symbol: str, notional_usd: float, confidence: float,
    reason: str, status: str, blocked_rule: str = "", detail: str = "",
    alpaca_order_id: str = "",
) -> None:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trader_orders
                        (mode, action, symbol, notional_usd, confidence, reason,
                         status, blocked_rule, detail, alpaca_order_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                    """,
                    (mode, action, symbol, notional_usd, confidence, reason,
                     status, blocked_rule, detail, alpaca_order_id),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"[storage] store_order error: {exc}")


def store_equity_snapshot(mode: str, equity_usd: float, cash_usd: float, positions: list[dict]) -> None:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trader_equity (mode, equity_usd, cash_usd, positions) VALUES (%s,%s,%s,%s);",
                    (mode, equity_usd, cash_usd, json.dumps(positions)),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"[storage] store_equity_snapshot error: {exc}")


def _rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_recent_orders(limit: int = 50) -> list[dict]:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM trader_orders ORDER BY created_at DESC LIMIT %s;", (limit,))
                return _rows_to_dicts(cur)
    except Exception as exc:
        logger.error(f"[storage] get_recent_orders error: {exc}")
        return []


def get_equity_curve(limit: int = 500) -> list[dict]:
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT created_at, mode, equity_usd, cash_usd FROM trader_equity "
                    "ORDER BY created_at DESC LIMIT %s;", (limit,))
                return _rows_to_dicts(cur)
    except Exception as exc:
        logger.error(f"[storage] get_equity_curve error: {exc}")
        return []
