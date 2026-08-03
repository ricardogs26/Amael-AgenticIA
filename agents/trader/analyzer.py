"""
analyzer.py — El LLM propone una operación (o hold) con justificación.

Usa ChatOllama con reasoning=False (mismo gotcha que camael_analyzer: qwen3 en
modo thinking bloquea la respuesta JSON). El output es SOLO una propuesta;
policy.py decide si se ejecuta.

OJO con los nombres de los parámetros: langchain_ollama usa extra="ignore", así
que un kwarg mal escrito se descarta en silencio y el modelo queda con su
comportamiento por defecto. El campo es `reasoning`, no `think`; y el timeout va
en `client_kwargs={"timeout": N}`, no en `request_timeout`.
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

# Segundos máximos por propuesta. Un ciclo normal tarda segundos; el margen es
# para cuando Ollama tiene que recargar el modelo (~40-60 s con qwen3:14b).
#
# Sin límite, ChatOllama espera indefinidamente: el 03-ago-2026 una llamada
# quedó colgada 11 min 37 s y devolvió vacío, más de lo que dura el intervalo
# del ciclo (600 s). Fallar rápido y reintentar en el siguiente ciclo pierde una
# propuesta; quedarse colgado bloquea el trader y satura la GPU.
TRADER_LLM_TIMEOUT_S = int(os.environ.get("TRADER_LLM_TIMEOUT", "180"))

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
EVENTOS MACRO (campo eventos_macro):
- Lista de publicaciones que mueven índices y cripto (CPI, empleo, PCE, GDP,
  FOMC) con `en_horas` e `impacto`. La whitelist son ETFs de índice y cripto:
  un dato macro les pega más fuerte que cualquier señal de momentum de 6h.
- Con un evento `high` a menos de 6 horas: no abras posiciones nuevas — la
  ganancia esperada de tu señal es menor que el gap potencial. Si ya tienes
  una posición en verde cerca de su target, cerrarla antes del evento es una
  decisión razonable; declárala así en el reason.
- Con un evento `high` a menos de 90 minutos el policy engine YA bloquea las
  compras, así que proponer buy solo desperdicia el ciclo. Las ventas siguen
  permitidas.
- Sin eventos cercanos, opera normal — no uses esto como excusa para no operar.

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
  agota, vender es buena decisión. Pero NO vendas en verde antes de tu
  target_pct salvo invalidación clara de la tesis — cortar ganancias temprano
  y dejar correr stops completos hace perdedor al sistema.
- RENTABILIDAD REAL: comisiones_pagadas_7d_usd es lo que Alpaca cobró de
  verdad. En cripto cada round-trip cuesta ~0.5% (taker 0.25% x2); tu
  movimiento esperado debe superar ese costo con margen. En equities el
  costo es ~$0 — para señales marginales prefiere equities sobre cripto.
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

    from agents.trader.broker import is_crypto

    # Con NYSE cerrada los equities salen de la whitelist y de las señales:
    # el LLM no puede proponer lo que no ve (determinista, no una sugerencia).
    operable = whitelist if market_open else [s for s in whitelist if is_crypto(s)]
    all_signals = compute_signals(bars, intraday or {})
    context = {
        "equity_usd": account.equity_usd,
        "cash_usd": account.cash_usd,
        "positions": account.positions,
        "whitelist": operable,
        "nyse_open": market_open,
        "nota": ("whitelist ya contiene SOLO los símbolos operables ahora mismo. "
                 "Posiciones de símbolos fuera de la whitelist no son operables en "
                 "este ciclo (NYSE cerrada) — no propongas nada sobre ellas."),
        "signals": {k: v for k, v in all_signals.items()
                    if k in operable or k.replace("/", "") in
                    [s.replace("/", "") for s in operable]},
    }
    try:
        from agents.trader import thesis as thesis_mod
        context["recent_trades"] = thesis_mod.recent_trades_summary(10)
        context["open_theses"] = thesis_mod.open_theses(account.positions)
    except Exception as exc:
        logger.warning(f"[analyzer] contexto de memoria no disponible: {exc}")
    try:
        from agents.trader.broker import get_fees_summary
        context["comisiones_pagadas_7d_usd"] = get_fees_summary(7)["total_fees_usd"]
    except Exception:
        pass
    try:
        from agents.trader import macro_calendar
        context["eventos_macro"] = macro_calendar.upcoming()
    except Exception as exc:
        logger.warning(f"[analyzer] calendario macro no disponible: {exc}")

    try:
        llm = ChatOllama(
            model=TRADER_LLM_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=0.2,
            # reasoning=False, NO think=False: langchain_ollama ignora los
            # campos desconocidos (extra="ignore"), así que think= no hacía nada
            # y qwen3 seguía razonando dentro del contenido, rompiendo el JSON.
            reasoning=False,
            format="json",  # Ollama fuerza salida JSON válida a nivel runtime
            # El timeout va al cliente HTTP; request_timeout tampoco existe.
            client_kwargs={"timeout": TRADER_LLM_TIMEOUT_S},
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
        if proposal.action in ("buy", "sell") and not proposal.symbol:
            proposal = TradeProposal(action="hold",
                                     reason=f"propuesta sin símbolo descartada: {proposal.reason[:200]}")
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
