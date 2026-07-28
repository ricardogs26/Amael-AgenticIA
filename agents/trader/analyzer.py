"""
analyzer.py — El LLM propone una operación (o hold) con justificación.

Usa ChatOllama con think=False (mismo gotcha que camael_analyzer: qwen3 en
modo thinking bloquea la respuesta JSON). El output es SOLO una propuesta;
policy.py decide si se ejecuta.
"""
from __future__ import annotations

import json
import logging
import os
import re

from agents.trader.models import AccountSnapshot, TradeProposal

logger = logging.getLogger("agents.trader.analyzer")

TRADER_LLM_MODEL = os.environ.get("TRADER_LLM_MODEL", "qwen3:14b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")

_SYSTEM = """Eres un analista de trading activo administrando una cuenta \
experimental. Tu objetivo es capturar movimientos de momentum intradía y \
tendencias de corto plazo, gestionando el riesgo con posiciones pequeñas.

Recibes barras diarias (14 días, campo daily_bars_14d) para la tendencia y \
barras de 15 minutos (últimas horas, campo intraday_bars_15m) para el momentum.

Responde ÚNICAMENTE con un objeto JSON, sin texto adicional:
{"action": "buy" | "sell" | "hold",
 "symbol": "<símbolo de la whitelist o vacío si hold>",
 "notional_usd": <monto en USD, 0 si hold>,
 "confidence": <0.0 a 1.0>,
 "reason": "<justificación breve en español, máx 2 frases>"}

Reglas:
- Solo símbolos de la whitelist proporcionada.
- Opera cuando haya una señal razonable (momentum intradía sostenido, rebote en
  soporte, ruptura con volumen); no necesitas certeza absoluta, el tamaño de la
  posición ya limita el riesgo. Si de plano no hay nada, hold.
- Toma utilidades: si una posición abierta tiene ganancia y el momentum se
  agota, vender es buena decisión.
- sell solo si existe posición abierta en ese símbolo.
- Evita sobreoperar el mismo símbolo en ciclos consecutivos sin cambio de señal.
- Calibra confidence honestamente: 0.5 = señal débil pero real, 0.7 = señal
  clara, 0.9 = señal muy fuerte con confirmación en ambas escalas de tiempo.
"""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"sin JSON en respuesta: {text[:200]}")
    return json.loads(m.group(0))


def propose_trade(
    account: AccountSnapshot,
    bars: dict[str, list[dict]],
    whitelist: list[str],
    market_open: bool,
    intraday: dict[str, list[dict]] | None = None,
) -> TradeProposal:
    """Un ciclo de análisis → una propuesta. Ante cualquier error → hold."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    context = {
        "equity_usd": account.equity_usd,
        "cash_usd": account.cash_usd,
        "positions": account.positions,
        "whitelist": whitelist,
        "nyse_open": market_open,
        "nota": "si nyse_open=false solo puedes operar símbolos cripto (con /)",
        "daily_bars_14d": bars,
        "intraday_bars_15m": intraday or {},
    }

    try:
        llm = ChatOllama(
            model=TRADER_LLM_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0.2,
            think=False,   # qwen3: thinking bloquea el modo JSON directo
            format="json",  # Ollama fuerza salida JSON válida a nivel runtime
        )
        resp = llm.invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
        ])
        data = _extract_json(resp.content)
        proposal = TradeProposal(
            action=str(data.get("action", "hold")).lower().strip(),
            symbol=str(data.get("symbol", "")).upper().strip(),
            notional_usd=float(data.get("notional_usd", 0) or 0),
            reason=str(data.get("reason", ""))[:500],
            confidence=max(0.0, min(1.0, float(data.get("confidence", 0) or 0))),
        )
        logger.info(
            f"[analyzer] propuesta: {proposal.action} {proposal.symbol} "
            f"${proposal.notional_usd:.2f} conf={proposal.confidence:.2f}"
        )
        return proposal
    except Exception as exc:
        logger.error(f"[analyzer] propose_trade error: {exc}", exc_info=True)
        return TradeProposal(action="hold", reason=f"error de análisis: {exc}")
