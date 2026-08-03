"""
chat.py — Chat conversacional con el agente trader.

Responde preguntas sobre la cuenta, órdenes y decisiones usando el LLM con
contexto en vivo. Los comandos de control (/halt, /resume, /cycle) NO pasan
por el LLM: se detectan determinísticamente en el endpoint y requieren
confirmación explícita del usuario.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("agents.trader.chat")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
CHAT_LLM_MODEL = os.environ.get("TRADER_CHAT_LLM_MODEL",
                                os.environ.get("TRADER_LLM_MODEL", "qwen3:14b"))

_SYSTEM = """Eres el agente de trading de Amael-IA. Operas una cuenta {mode} en
Alpaca con un loop autónomo: cada ciclo el LLM propone y un policy engine
determinista aprueba o bloquea (límites duros que no puedes saltarte).

Recibes tu estado en vivo en JSON: cuenta, posiciones, órdenes recientes
(con la razón de cada decisión), límites y señales de mercado.

Responde en español de México, breve y directo. Cita números concretos del
contexto. Si te preguntan por qué hiciste algo, usa el campo "reason" de la
orden correspondiente. Si te piden ejecutar acciones (parar, reanudar,
operar), indica que usen los comandos /halt, /resume o /cycle — tú no puedes
ejecutarlas desde el chat. No inventes datos que no estén en el contexto."""


def build_context() -> dict:
    """Estado en vivo del agente para inyectar al LLM."""
    from agents.trader import policy
    from agents.trader.broker import TRADER_MODE, get_account_snapshot
    from agents.trader.storage import get_recent_orders

    ctx: dict = {
        "mode": TRADER_MODE,
        "halted": policy.is_halted(),
        "circuit_breaker_paused": policy.is_cb_paused(),
        "orders_today": policy.orders_today(),
        "limits": {
            "max_order_usd": policy.MAX_ORDER_USD,
            "max_orders_per_day": policy.MAX_ORDERS_PER_DAY,
            "max_symbol_exposure_usd": policy.MAX_SYMBOL_EXPOSURE_USD,
            "min_momentum_pct": policy.MIN_MOMENTUM_PCT,
            "min_momentum_pct_crypto": policy.MIN_MOMENTUM_PCT_CRYPTO,
            "halt_equity_usd": policy.HALT_EQUITY_USD,
            "min_confidence": policy.MIN_CONFIDENCE,
            "whitelist": policy.SYMBOL_WHITELIST,
        },
    }
    try:
        snap = get_account_snapshot()
        ctx["account"] = {
            "equity_usd": snap.equity_usd,
            "cash_usd": snap.cash_usd,
            "positions": snap.positions,
        }
    except Exception as exc:
        ctx["account"] = {"error": str(exc)[:200]}
    try:
        ctx["recent_orders"] = get_recent_orders(15)
    except Exception as exc:
        ctx["recent_orders"] = [{"error": str(exc)[:200]}]
    return ctx


def answer(message: str, history: list[dict] | None = None) -> str:
    """Responde una pregunta sobre el estado/decisiones del trader."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    ctx = build_context()
    llm = ChatOllama(
        model=CHAT_LLM_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0.3,
        reasoning=False,
    )
    msgs = [
        SystemMessage(content=_SYSTEM.format(mode=ctx["mode"])),
        SystemMessage(content="Estado en vivo:\n" + json.dumps(ctx, ensure_ascii=False,
                                                               default=str)),
    ]
    for turn in (history or [])[-6:]:
        role, content = turn.get("role"), str(turn.get("content", ""))[:1000]
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    msgs.append(HumanMessage(content=message[:2000]))

    resp = llm.invoke(msgs)
    return str(resp.content).strip()
