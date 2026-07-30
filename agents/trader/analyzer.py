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

Recibes por símbolo INDICADORES YA CALCULADOS (campo signals):
  price       — precio actual
  pct_1h      — cambio % última hora
  pct_6h      — cambio % últimas 6 horas
  pct_24h     — cambio % último día
  pct_14d     — cambio % últimos 14 días
  vs_sma14    — % del precio actual sobre/bajo el promedio de 14 días

Guía de lectura:
  pct_6h > +0.7 con pct_1h positivo  → momentum alcista real (candidato buy)
    · en CRIPTO exige pct_6h > +1.2: el round-trip cuesta ~0.5% (fee taker
      0.25% x2 + spread) — una señal menor no cubre la fricción
  vs_sma14 < -2 con pct_1h volteando a positivo → posible rebote (candidato buy)
  todo entre ±0.3                    → mercado plano, hold
(Los volúmenes cripto del feed no son confiables — ignóralos; decide por precio.)

MEMORIA (campos recent_trades y open_theses):
- open_theses: tu plan declarado por posición abierta (thesis, target_pct,
  stop_pct, pl_pct_vs_entry, target_alcanzado, stop_alcanzado). Las posiciones
  se evalúan CONTRA SU TESIS, no contra las señales genéricas: vende si
  target_alcanzado=true, si stop_alcanzado=true, o si la razón de entrada ya
  se invalidó. USA los booleanos precalculados, no compares los números tú.
  Una posición de rebote NACE con pct_6h negativo — no es razón para venderla.
- recent_trades: tus últimas órdenes con minutos transcurridos. Si vendiste
  un símbolo hace poco, NO reentres solo porque la misma señal sigue ahí:
  exige que algo haya cambiado (señal más fuerte, precio mejor que tu salida).
  Round-trips repetidos del mismo símbolo con ganancia decreciente = ruido.

Responde ÚNICAMENTE con un objeto JSON, sin texto adicional:
{"action": "buy" | "sell" | "hold",
 "symbol": "<símbolo de la whitelist o vacío si hold>",
 "notional_usd": <monto en USD, 0 si hold>,
 "confidence": <0.0 a 1.0>,
 "reason": "<justificación breve en español citando los indicadores, máx 2 frases>",
 "exit_plan": {"thesis": "<solo en buy: por qué entras y qué esperas>",
               "target_pct": <solo en buy: % de ganancia para salir, ej 2.0>,
               "stop_pct": <solo en buy: % de pérdida para cortar, ej -1.5>}}

Reglas:
- Solo símbolos de la whitelist proporcionada.
- Opera cuando haya una señal razonable; no necesitas certeza absoluta, el
  tamaño de la posición ya limita el riesgo. Si todo está plano, hold.
- Toma utilidades: si una posición abierta tiene ganancia y el momentum se
  agota, vender es buena decisión.
- sell solo si existe posición abierta en ese símbolo.
- Calibra confidence honestamente: 0.5 = señal débil pero real, 0.7 = señal
  clara, 0.9 = señal muy fuerte con confirmación en varias escalas de tiempo.
"""


def _pct(a: float, b: float) -> float | None:
    """% de cambio de b→a; None si no hay base."""
    return round((a - b) / b * 100, 2) if b else None


def compute_signals(
    bars: dict[str, list[dict]], intraday: dict[str, list[dict]]
) -> dict[str, dict]:
    """Indicadores deterministas por símbolo — el LLM decide, no calcula."""
    signals: dict[str, dict] = {}
    for sym in set(bars) | set(intraday):
        d = bars.get(sym, [])
        i = intraday.get(sym, [])
        price = i[-1]["c"] if i else (d[-1]["c"] if d else None)
        if price is None:
            continue
        sma14 = round(sum(b["c"] for b in d) / len(d), 2) if d else None
        signals[sym] = {
            "price": price,
            "pct_1h": _pct(price, i[-5]["c"]) if len(i) >= 5 else None,
            "pct_6h": _pct(price, i[0]["c"]) if len(i) >= 2 else None,
            "pct_24h": _pct(price, d[-2]["c"]) if len(d) >= 2 else None,
            "pct_14d": _pct(price, d[0]["c"]) if len(d) >= 2 else None,
            "vs_sma14": _pct(price, sma14) if sma14 else None,
        }
    return signals


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
        "nota": ("si nyse_open=false NO propongas órdenes de equities (ni buy ni sell "
                 "— se bloquearían); evalúalos hasta que abra NYSE. Solo cripto (con /) "
                 "opera 24/7."),
        "signals": compute_signals(bars, intraday or {}),
    }
    try:
        from agents.trader import thesis as thesis_mod
        context["recent_trades"] = thesis_mod.recent_trades_summary(10)
        context["open_theses"] = thesis_mod.open_theses(account.positions)
    except Exception as exc:
        logger.warning(f"[analyzer] contexto de memoria no disponible: {exc}")

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
        plan = data.get("exit_plan") or {}
        if isinstance(plan, dict) and proposal.action == "buy":
            proposal.exit_thesis = str(plan.get("thesis", ""))[:500]
            try:
                proposal.target_pct = float(plan["target_pct"])
            except (KeyError, TypeError, ValueError):
                proposal.target_pct = 2.0
            try:
                proposal.stop_pct = float(plan["stop_pct"])
            except (KeyError, TypeError, ValueError):
                proposal.stop_pct = -1.5
        logger.info(
            f"[analyzer] propuesta: {proposal.action} {proposal.symbol} "
            f"${proposal.notional_usd:.2f} conf={proposal.confidence:.2f} — {proposal.reason[:150]}"
        )
        return proposal
    except Exception as exc:
        logger.error(f"[analyzer] propose_trade error: {exc}", exc_info=True)
        return TradeProposal(action="hold", reason=f"error de análisis: {exc}")
