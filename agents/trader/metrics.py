"""Métricas Prometheus del trader (expuestas en /metrics de trader-service)."""
from prometheus_client import Counter, Gauge, Histogram

TRADER_LOOP_RUNS = Counter(
    "amael_trader_loop_runs_total",
    "Ciclos del loop del trader por resultado",
    ["mode", "result"],  # result: ok | hold | halted | cb_paused | error
)

TRADER_ORDERS = Counter(
    "amael_trader_orders_total",
    "Decisiones de orden por estado",
    ["mode", "action", "status"],  # status: executed | blocked | pending_approval | error
)

TRADER_POLICY_BLOCKS = Counter(
    "amael_trader_policy_blocks_total",
    "Propuestas bloqueadas por regla del policy engine",
    ["mode", "rule"],
)

TRADER_EQUITY = Gauge(
    "amael_trader_equity_usd",
    "Equity actual de la cuenta",
    ["mode"],
)

TRADER_CASH = Gauge(
    "amael_trader_cash_usd",
    "Cash disponible en la cuenta",
    ["mode"],
)

TRADER_CONFIDENCE = Histogram(
    "amael_trader_llm_confidence",
    "Confianza de las propuestas del LLM",
    ["mode"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
