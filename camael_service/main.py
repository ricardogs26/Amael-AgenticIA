"""
main.py — Entry point de camael-service (DevOps / GitOps Agent standalone).

camael-service es un microservicio FastAPI independiente que empaqueta
la lógica del agente Camael (agents.devops) extraída del backend
amael-agentic-backend en Fase 3.

Expone:
  - POST /api/camael/handoff      ← Raphael dispara handoff GitOps
  - PATCH /api/camael/rfc/{sys_id} ← Raphael update RFC post-verif
  - POST /api/devops/ci-hook      ← GitHub webhook (llega aquí en Fase 4+; en Fase 3 sigue en backend)
  - POST /api/devops/webhook/bitbucket ← Bitbucket webhook (idem)
  - GET /health                   ← Liveness/Readiness
  - GET /metrics                  ← Prometheus metrics

Secuencia de arranque:
  1. Logging estructurado
  2. PostgreSQL pool
  3. Redis client
  4. Drain WAL pendiente (handoffs + rfc_update que quedaron en Redis mientras
     Camael estaba caído — se procesan ANTES de aceptar tráfico nuevo)
  5. APScheduler con tick cada 5min para re-drenar WAL (backup ante nuevos fallos)
  6. OTel instrumentation + Prometheus metrics

Arranque:
    uvicorn camael_service.main:app --host 0.0.0.0 --port 8003
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from observability.logging import setup_logging

setup_logging()
logger = logging.getLogger("camael_service.main")

_scheduler = None  # APScheduler instance (module-level para shutdown clean)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown de camael-service."""
    global _scheduler

    logger.info("=== camael-service iniciando ===")
    from config.settings import settings

    # 1. PostgreSQL pool
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

    # 2. Redis
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

    # 3. Drain WAL pendiente (best-effort; fallos no bloquean startup)
    try:
        await _drain_wal_local()
    except Exception as exc:
        logger.warning(f"[startup] drain WAL inicial falló: {exc}")

    # 4. APScheduler tick cada 5min
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(
            _drain_wal_local,
            trigger=IntervalTrigger(minutes=5),
            id="camael-wal-drain",
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logger.info("[startup] APScheduler tick 5min registrado")
    except Exception as exc:
        logger.warning(f"[startup] APScheduler falló: {exc}")

    # 5. OTel
    try:
        from observability.tracing import instrument_app, instrument_requests
        instrument_app(app)
        instrument_requests()
        logger.info("[startup] OpenTelemetry instrumentado")
    except Exception as exc:
        logger.warning(f"[startup] OTel falló: {exc}")

    logger.info("=== camael-service listo en :8003 ===")
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("=== camael-service apagando ===")
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
    try:
        from storage.postgres.client import close_pool
        close_pool()
    except Exception:
        pass
    logger.info("=== camael-service apagado ===")


async def _drain_wal_local() -> None:
    """
    Drena entradas WAL procesándolas localmente (Camael corre aquí in-process).

    Handoffs: llama a agents.devops.agent.handle_handoff directamente.
    RFC updates: llama a agents.devops.servicenow_client directamente.
    """
    from storage.redis import wal

    # ── Handoffs ─────────────────────────────────────────────────────────────
    def _consume_handoff(payload: dict) -> bool:
        try:
            from agents.devops.agent import handle_handoff
            # Sync wrapper para llamar async desde sync consumer
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(handle_handoff(payload))
            finally:
                loop.close()
            # None = issue no soportado; lo damos por drenado para no reintentar.
            return True
        except Exception as exc:
            logger.warning(f"[wal-drain] handoff FALLÓ: {exc}")
            return False

    # ── RFC updates ──────────────────────────────────────────────────────────
    def _consume_rfc(payload: dict) -> bool:
        try:
            from agents.devops import servicenow_client as sn
            if not sn.is_configured():
                return False  # retry when SN is back
            sys_id = payload.get("_sys_id") or payload.get("sys_id")
            if not sys_id:
                return True  # malformed — drop
            result = payload.get("result")
            message = payload.get("message", "")
            loop = asyncio.new_event_loop()
            try:
                if result == "closed":
                    loop.run_until_complete(sn.close_rfc(sys_id, message))
                elif result == "review":
                    loop.run_until_complete(sn.fail_rfc(sys_id, message))
                else:
                    logger.warning(f"[wal-drain] rfc_update result inválido: {result}")
                    return True
            finally:
                loop.close()
            return True
        except Exception as exc:
            logger.warning(f"[wal-drain] rfc_update FALLÓ: {exc}")
            return False

    drained_h = wal.drain("handoff", _consume_handoff)
    drained_r = wal.drain("rfc_update", _consume_rfc)
    if drained_h or drained_r:
        logger.info(f"[wal-drain] handoff={drained_h} rfc_update={drained_r}")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    from config.settings import settings

    app = FastAPI(
        title="camael-service",
        description=(
            "Microservicio DevOps/GitOps. Absorbe agents/devops/ del backend. "
            "Expone /api/camael/* (handoff + rfc update) y /api/devops/* (webhooks)."
        ),
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    from observability.middleware import ObservabilityMiddleware
    app.add_middleware(ObservabilityMiddleware)

    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Health — usa el builder estándar del monorepo (chequea PG/Redis/Qdrant/Ollama)
    from observability.health import build_health_router
    hr = build_health_router()
    if hr is not None:
        app.include_router(hr)
    else:
        @app.get("/health")
        def _fallback_health():
            return {"status": "ok", "service": "camael-service"}

    # Router interno Raphael ↔ Camael (handoff + rfc update)
    from interfaces.api.routers.camael import router as camael_router
    app.include_router(camael_router)

    # Router webhooks externos (GitHub, Bitbucket) — se registra siempre.
    # En Fase 3 los webhooks llegan al backend por ingress; montarlos aquí
    # prepara el terreno para Fase 4+ cuando se migre el ingress.
    from interfaces.api.routers.devops import router as devops_router
    app.include_router(devops_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "camael_service.main:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
        log_config=None,
    )
