"""
agents.trader — Agente de trading experimental (paper/live vía Alpaca).

Arquitectura: el LLM (analyzer) PROPONE, el policy engine determinista DISPONE.
El LLM nunca toca la API del broker directamente ni puede saltarse límites.

Módulos:
  broker.py    — wrapper alpaca-py (paper/live según TRADER_MODE)
  policy.py    — guardrails deterministas (límites duros, halt, circuit breaker)
  analyzer.py  — qwen3:14b propone {action, symbol, notional_usd, reason, confidence}
  storage.py   — tablas trader_orders / trader_equity en PostgreSQL
  reporter.py  — notificaciones WhatsApp (reporte diario + alertas)
  loop.py      — APScheduler: ciclo observe → propose → policy → execute → report
"""
from agents.trader.loop import start_trader_loop, stop_trader_loop
from agents.trader.storage import init_trader_db

__all__ = ["start_trader_loop", "stop_trader_loop", "init_trader_db"]
