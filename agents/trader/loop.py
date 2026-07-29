"""
loop.py — Ciclo autónomo del trader (APScheduler).

Cada TRADER_LOOP_INTERVAL segundos (default 900 = 15 min):
  observe (cuenta + barras) → analyzer propone → policy evalúa → execute → persist

Reporte diario por WhatsApp a las 15:10 America/Mexico_City (tras cierre NYSE).
"""
from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("agents.trader.loop")

LOOP_INTERVAL = int(os.environ.get("TRADER_LOOP_INTERVAL", "900"))
LOOP_ENABLED = os.environ.get("TRADER_LOOP_ENABLED", "true").lower() == "true"

_scheduler: BackgroundScheduler | None = None


def trader_cycle() -> None:
    """Un ciclo completo. Cualquier excepción se registra y no tumba el scheduler."""
    from agents.trader import analyzer, broker, policy, reporter, storage
    from agents.trader.metrics import (
        TRADER_CASH, TRADER_CONFIDENCE, TRADER_EQUITY, TRADER_LAST_CONFIDENCE,
        TRADER_LOOP_RUNS,
        TRADER_ORDERS, TRADER_POLICY_BLOCKS,
    )

    mode = broker.TRADER_MODE
    try:
        # 1. Observe
        account = broker.get_account_snapshot()
        storage.store_equity_snapshot(mode, account.equity_usd, account.cash_usd, account.positions)
        TRADER_EQUITY.labels(mode=mode).set(account.equity_usd)
        TRADER_CASH.labels(mode=mode).set(account.cash_usd)

        # 2. Chequeo de HALT por equity ANTES de pensar (y notificar una sola vez)
        if account.equity_usd < policy.HALT_EQUITY_USD and not policy.is_halted():
            policy.set_halt(f"equity ${account.equity_usd:.2f} < ${policy.HALT_EQUITY_USD:.2f}")
            reporter.notify_whatsapp(
                f"🛑 *Trader HALT*: equity ${account.equity_usd:.2f} cayó bajo el "
                f"límite ${policy.HALT_EQUITY_USD:.2f}. Loop detenido hasta revisión manual."
            )
        if policy.is_halted():
            TRADER_LOOP_RUNS.labels(mode=mode, result="halted").inc()
            return
        if policy.is_cb_paused():
            TRADER_LOOP_RUNS.labels(mode=mode, result="cb_paused").inc()
            return

        # Cupo diario agotado → no gastar una llamada al LLM ni guardar filas bloqueadas
        if policy.orders_today() >= policy.MAX_ORDERS_PER_DAY:
            logger.info(f"[loop] cupo diario agotado ({policy.MAX_ORDERS_PER_DAY}) — skip análisis")
            TRADER_LOOP_RUNS.labels(mode=mode, result="daily_limit").inc()
            return

        # 3. Analyze — el LLM propone
        market_open = broker.market_is_open()
        bars = broker.get_recent_bars(policy.SYMBOL_WHITELIST)
        intraday = broker.get_intraday_bars(policy.SYMBOL_WHITELIST)
        proposal = analyzer.propose_trade(account, bars, policy.SYMBOL_WHITELIST, market_open,
                                          intraday=intraday)
        TRADER_CONFIDENCE.labels(mode=mode).observe(proposal.confidence)
        TRADER_LAST_CONFIDENCE.labels(mode=mode, action=proposal.action).set(proposal.confidence)

        # NYSE cerrada → solo cripto puede ejecutarse
        if proposal.action in ("buy", "sell") and not broker.is_crypto(proposal.symbol) and not market_open:
            storage.store_order(mode, proposal.action, proposal.symbol, proposal.notional_usd,
                                proposal.confidence, proposal.reason, "blocked",
                                blocked_rule="market_closed", detail="NYSE cerrada")
            TRADER_POLICY_BLOCKS.labels(mode=mode, rule="market_closed").inc()
            TRADER_LOOP_RUNS.labels(mode=mode, result="hold").inc()
            return

        # Sell sin monto (el LLM a veces manda $0) → default: toda la posición
        if proposal.action == "sell" and proposal.notional_usd <= 0:
            plain = proposal.symbol.upper().replace("/", "")
            pos = next((x for x in account.positions if x["symbol"].upper() == plain), None)
            if pos:
                proposal.notional_usd = round(pos["market_value"], 2)
                logger.info(f"[loop] sell sin notional — default a posición completa "
                            f"${proposal.notional_usd:.2f}")

        # 4. Policy — determinista (signals para la regla de momentum mínimo)
        decision = policy.evaluate(proposal, account,
                                   signals=analyzer.compute_signals(bars, intraday or {}))

        if decision.requires_approval:
            storage.store_order(mode, proposal.action, proposal.symbol, proposal.notional_usd,
                                proposal.confidence, proposal.reason, "pending_approval",
                                detail=decision.detail)
            TRADER_ORDERS.labels(mode=mode, action=proposal.action, status="pending_approval").inc()
            reporter.notify_whatsapp(
                f"⚠️ *Trader*: orden {proposal.action} {proposal.symbol} "
                f"${proposal.notional_usd:.2f} requiere tu aprobación ({decision.detail}).\n"
                f"Razón LLM: {proposal.reason}"
            )
            TRADER_LOOP_RUNS.labels(mode=mode, result="ok").inc()
            return

        if not decision.approved:
            if decision.blocked_rule != "hold":
                storage.store_order(mode, proposal.action, proposal.symbol, proposal.notional_usd,
                                    proposal.confidence, proposal.reason, "blocked",
                                    blocked_rule=decision.blocked_rule, detail=decision.detail)
                TRADER_POLICY_BLOCKS.labels(mode=mode, rule=decision.blocked_rule).inc()
                logger.info(f"[loop] Bloqueada por {decision.blocked_rule}: {decision.detail}")
            TRADER_LOOP_RUNS.labels(mode=mode, result="hold").inc()
            return

        # 5. Execute — único camino hacia el broker
        try:
            if proposal.action == "sell":
                result = broker.submit_sell_order(proposal.symbol, proposal.notional_usd,
                                                  account.positions)
            else:
                result = broker.submit_notional_order(proposal.symbol, proposal.action,
                                                      proposal.notional_usd)
            policy.count_order()
            storage.store_order(mode, proposal.action, proposal.symbol, proposal.notional_usd,
                                proposal.confidence, proposal.reason, "executed",
                                alpaca_order_id=result["id"], detail=result["status"])
            # Memoria de tesis: el plan de salida vive con la posición
            from agents.trader import thesis as thesis_mod
            if proposal.action == "buy":
                thesis_mod.save_thesis(proposal.symbol,
                                       proposal.exit_thesis or proposal.reason,
                                       proposal.target_pct or 2.0,
                                       proposal.stop_pct or -1.5)
            else:
                thesis_mod.clear_thesis(proposal.symbol)
            TRADER_ORDERS.labels(mode=mode, action=proposal.action, status="executed").inc()
            reporter.notify_whatsapp(
                f"✅ *Trader ({mode})*: {proposal.action} {proposal.symbol} "
                f"${proposal.notional_usd:.2f} ejecutada.\nRazón: {proposal.reason}"
            )
        except Exception as exc:
            storage.store_order(mode, proposal.action, proposal.symbol, proposal.notional_usd,
                                proposal.confidence, proposal.reason, "error", detail=str(exc)[:300])
            TRADER_ORDERS.labels(mode=mode, action=proposal.action, status="error").inc()
            logger.error(f"[loop] submit_order error: {exc}", exc_info=True)

        TRADER_LOOP_RUNS.labels(mode=mode, result="ok").inc()

    except Exception as exc:
        TRADER_LOOP_RUNS.labels(mode=mode, result="error").inc()
        logger.error(f"[loop] trader_cycle error: {exc}", exc_info=True)


def start_trader_loop() -> None:
    global _scheduler
    if not LOOP_ENABLED:
        logger.info("[loop] TRADER_LOOP_ENABLED=false — loop desactivado")
        return
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(trader_cycle, "interval", seconds=LOOP_INTERVAL,
                       id="trader_cycle", max_instances=1, coalesce=True)
    # Reporte diario 15:10 CDMX (NYSE cierra 15:00 CDMX en horario estándar)
    from agents.trader.reporter import daily_report, morning_report
    _scheduler.add_job(daily_report, "cron", hour=15, minute=10,
                       timezone="America/Mexico_City", id="trader_daily_report")
    # Resumen nocturno 7:00 CDMX — solo si hubo movimientos (órdenes o ±0.1% equity)
    _scheduler.add_job(morning_report, "cron", hour=7, minute=0,
                       timezone="America/Mexico_City", id="trader_morning_report")
    _scheduler.start()
    logger.info(f"[loop] Trader loop iniciado (interval={LOOP_INTERVAL}s)")


def stop_trader_loop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[loop] Trader loop detenido")
