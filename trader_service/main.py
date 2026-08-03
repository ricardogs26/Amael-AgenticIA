"""
main.py — Entry point de trader-service (agente de trading experimental).

Microservicio FastAPI independiente (mismo patrón que raphael-service) que
empaqueta agents.trader: loop autónomo Alpaca paper/live con policy engine
determinista + auditoría en PostgreSQL + reportes WhatsApp.

Arranque:
    uvicorn trader_service.main:app --host 0.0.0.0 --port 8003

Puerto: 8003. Acceso: interno al cluster (INTERNAL_API_SECRET); el frontend
lo consumirá vía proxy del backend en fase 2.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from prometheus_client import make_asgi_app

from observability.logging import setup_logging

setup_logging()

logger = logging.getLogger("trader_service.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("=== trader-service iniciando ===")

    from config.settings import settings

    try:
        from storage.postgres.client import init_pool
        init_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            min_conn=settings.postgres_pool_min,
            max_conn=settings.postgres_pool_max,
        )
        logger.info("[startup] PostgreSQL pool inicializado")
    except Exception as exc:
        logger.error(f"[startup] PostgreSQL FALLÓ: {exc}", exc_info=True)

    try:
        from storage.redis.client import init_client
        init_client(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
        )
        logger.info("[startup] Redis client inicializado")
    except Exception as exc:
        logger.error(f"[startup] Redis FALLÓ: {exc}", exc_info=True)

    try:
        from agents.trader import init_trader_db, start_trader_loop
        init_trader_db()
        start_trader_loop()
        from agents.trader.broker import TRADER_MODE
        logger.info(f"[startup] Trader loop iniciado (mode={TRADER_MODE})")
    except Exception as exc:
        logger.error(f"[startup] Trader agent FALLÓ: {exc}", exc_info=True)

    try:
        from observability.tracing import instrument_app, instrument_requests
        instrument_app(app)
        instrument_requests()
        logger.info("[startup] OpenTelemetry instrumentado")
    except Exception as exc:
        logger.warning(f"[startup] OTel instrumentation falló: {exc}")

    logger.info("=== trader-service listo en :8003 ===")

    yield

    logger.info("=== trader-service apagando ===")
    import asyncio
    await asyncio.sleep(5)

    try:
        from agents.trader import stop_trader_loop
        stop_trader_loop()
    except Exception as exc:
        logger.warning(f"[shutdown] Trader loop stop falló: {exc}")

    try:
        from storage.postgres.client import close_pool
        close_pool()
    except Exception as exc:
        logger.warning(f"[shutdown] PostgreSQL close falló: {exc}")

    logger.info("=== trader-service apagado ===")


def create_app() -> FastAPI:
    from config.settings import settings

    app = FastAPI(
        title="trader-service",
        description=(
            "Agente de trading experimental (Alpaca paper/live). "
            "LLM propone, policy engine determinista dispone."
        ),
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    from observability.middleware import ObservabilityMiddleware
    app.add_middleware(ObservabilityMiddleware)

    app.mount("/metrics", make_asgi_app())

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "trader-service"}

    _register_api(app)
    return app


def _register_api(app: FastAPI) -> None:
    """Endpoints /api/trader/* — protegidos con INTERNAL_API_SECRET."""
    from interfaces.api.auth import require_internal_secret

    deps = [Depends(require_internal_secret)]

    @app.get("/api/trader/status", tags=["trader"], dependencies=deps)
    def status() -> dict:
        from agents.trader import policy
        from agents.trader.broker import TRADER_MODE
        from agents.trader.loop import LOOP_ENABLED, LOOP_INTERVAL
        return {
            "mode": TRADER_MODE,
            "loop_enabled": LOOP_ENABLED,
            "loop_interval_s": LOOP_INTERVAL,
            "halted": policy.is_halted(),
            "circuit_breaker_paused": policy.is_cb_paused(),
            "orders_today": policy.orders_today(),
            "limits": {
                "max_order_usd": policy.MAX_ORDER_USD,
                "max_orders_per_day": policy.MAX_ORDERS_PER_DAY,
                "halt_equity_usd": policy.HALT_EQUITY_USD,
                "min_confidence": policy.MIN_CONFIDENCE,
                "whitelist": policy.SYMBOL_WHITELIST,
            },
        }

    @app.get("/api/trader/account", tags=["trader"], dependencies=deps)
    def account() -> dict:
        from agents.trader.broker import TRADER_MODE, get_account_snapshot
        try:
            snap = get_account_snapshot()
            return {
                "mode": TRADER_MODE,
                "equity_usd": snap.equity_usd,
                "cash_usd": snap.cash_usd,
                "positions": snap.positions,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"alpaca_error: {exc}") from exc

    @app.get("/api/trader/orders", tags=["trader"], dependencies=deps)
    def orders(limit: int = 50) -> dict:
        from agents.trader.storage import get_recent_orders
        return {"orders": get_recent_orders(min(limit, 200))}

    @app.get("/api/trader/equity", tags=["trader"], dependencies=deps)
    def equity(limit: int = 500) -> dict:
        from agents.trader.storage import get_equity_curve
        return {"curve": get_equity_curve(min(limit, 2000))}

    @app.get("/api/trader/fees", tags=["trader"], dependencies=deps)
    def fees(days: int = 7) -> dict:
        from agents.trader.broker import get_fees_summary
        try:
            return get_fees_summary(min(days, 90))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"alpaca_error: {exc}") from exc

    @app.get("/api/trader/calendar", tags=["trader"], dependencies=deps)
    def calendar(hours: int | None = None, refresh: bool = False) -> dict:
        """Eventos macro próximos + estado de blackout (diagnóstico)."""
        from agents.trader import macro_calendar
        if refresh:
            macro_calendar.refresh()
        return {
            "fred_configured": bool(macro_calendar.FRED_API_KEY),
            "blackout_min": macro_calendar.BLACKOUT_MIN,
            "blackout_after_min": macro_calendar.BLACKOUT_AFTER_MIN,
            "blackout_activo": macro_calendar.blackout(),
            "eventos": macro_calendar.upcoming(hours=hours),
        }

    @app.post("/api/trader/halt", tags=["trader"], dependencies=deps)
    def halt() -> dict:
        from agents.trader import policy
        policy.set_halt("manual via API")
        return {"halted": True}

    @app.post("/api/trader/resume", tags=["trader"], dependencies=deps)
    def resume() -> dict:
        from agents.trader import policy
        policy.clear_halt()
        return {"halted": False}

    @app.post("/api/trader/run-cycle", tags=["trader"], dependencies=deps)
    def run_cycle() -> dict:
        """Dispara un ciclo manual (útil para pruebas sin esperar el interval)."""
        from agents.trader.loop import trader_cycle
        trader_cycle()
        return {"status": "cycle_executed"}

    @app.post("/api/trader/chat", tags=["trader"], dependencies=deps)
    def chat(payload: dict) -> dict:
        """
        Chat con el agente. Comandos (/halt, /resume, /cycle) se ejecutan
        determinísticamente y requieren confirm=true; el resto va al LLM
        con contexto en vivo.
        """
        from agents.trader import policy
        from agents.trader import chat as trader_chat

        message = str(payload.get("message", "")).strip()
        confirm = bool(payload.get("confirm", False))
        if not message:
            raise HTTPException(status_code=422, detail="message vacío")

        cmd = message.lower().split()[0]
        commands = {"/halt": "detener el loop (HALT manual)",
                    "/resume": "reanudar el loop (quitar HALT)",
                    "/cycle": "forzar un ciclo de análisis ahora"}
        if cmd in commands:
            if not confirm:
                return {"reply": f"Vas a {commands[cmd]}. ¿Confirmas?",
                        "needs_confirm": True, "command": cmd}
            if cmd == "/halt":
                policy.set_halt("manual via chat")
                return {"reply": "🛑 Loop detenido (HALT manual). Usa /resume para reanudar.",
                        "needs_confirm": False}
            if cmd == "/resume":
                policy.clear_halt()
                return {"reply": "▶️ Loop reanudado.", "needs_confirm": False}
            from agents.trader.loop import trader_cycle
            trader_cycle()
            return {"reply": "🔄 Ciclo ejecutado — revisa las órdenes para ver qué decidió.",
                    "needs_confirm": False}

        try:
            reply = trader_chat.answer(message, payload.get("history"))
        except Exception as exc:
            logger.error(f"[chat] error: {exc}", exc_info=True)
            raise HTTPException(status_code=502, detail=f"llm_error: {exc}") from exc
        return {"reply": reply, "needs_confirm": False}


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("trader_service.main:app", host="0.0.0.0", port=8003,
                reload=True, log_config=None)
