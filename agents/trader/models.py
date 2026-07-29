"""Modelos de datos del agente trader."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeProposal:
    """Propuesta del LLM — todavía sin validar por el policy engine."""
    action: str                 # "buy" | "sell" | "hold"
    symbol: str = ""            # ej. "SPY", "BTC/USD"
    notional_usd: float = 0.0   # monto en USD (fracciones permitidas)
    reason: str = ""            # justificación del LLM (se persiste para auditoría)
    confidence: float = 0.0     # 0.0–1.0
    # Plan de salida declarado al comprar (memoria de tesis — fix del churn)
    exit_thesis: str = ""
    target_pct: float | None = None
    stop_pct: float | None = None


@dataclass
class PolicyDecision:
    """Veredicto determinista del policy engine sobre una propuesta."""
    approved: bool
    requires_approval: bool = False   # dentro de límites pero > umbral humano
    blocked_rule: str = ""            # regla que bloqueó (si approved=False)
    detail: str = ""


@dataclass
class AccountSnapshot:
    """Estado de la cuenta al inicio de cada ciclo."""
    equity_usd: float
    cash_usd: float
    positions: list[dict] = field(default_factory=list)  # [{symbol, qty, market_value, unrealized_pl}]
