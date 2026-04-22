"""
main.py — Entry point de raphael-service (SRE autónomo standalone).

raphael-service es un microservicio FastAPI independiente que empaqueta
la lógica del agente SRE (agents.sre) y absorbe las rutas del legacy
k8s-agent 5.2.0 (`/api/k8s-agent` conversacional).

Secuencia de arranque:
  1. Logging estructurado (JSON en prod, legible en dev)
  2. PostgreSQL pool (sre_incidents, sre_postmortems, sre_learning_stats)
  3. Redis client (dedup, maintenance windows, deploy hooks)
  4. SRE: init_sre_db, init_runbooks_qdrant (thread), start_sre_loop
  5. OTel instrumentation + Prometheus metrics endpoint

Arranque:
    uvicorn raphael_service.main:app --host 0.0.0.0 --port 8002

En Kubernetes usa un Deployment separado (fase 2.3) con imagen
    registry.richardx.dev/raphael-service:<version>.

Puerto: 8002 (mismo que el legacy k8s-agent para drop-in replacement).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

# ── Logging primero — antes de cualquier otro import que loguee ──────────────
from observability.logging import setup_logging

setup_logging()

logger = logging.getLogger("raphael_service.main")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup / shutdown de raphael-service.
    Versión reducida del lifespan del backend: sólo inicializa lo que el SRE
    agent necesita (Postgres, Redis, SRE loop, runbooks Qdrant).
    """
    # ── STARTUP ───────────────────────────────────────────────────────────────
    logger.info("=== raphael-service iniciando ===")

    from config.settings import settings

    # 1. PostgreSQL pool — sre_incidents, sre_postmortems, learning_stats
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

    # 2. Redis — dedup de incidentes, maintenance windows, deploy hooks
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

    # 3. SRE agent — DB schema, runbooks indexing, autonomous loop
    try:
        from agents.sre import init_runbooks_qdrant, init_sre_db, start_sre_loop
        init_sre_db()
        # Indexación de runbooks es síncrona y costosa → thread
        await _run_in_thread(init_runbooks_qdrant)
        start_sre_loop()
        logger.info("[startup] SRE agent iniciado (loop, db, runbooks)")
    except Exception as exc:
        logger.error(f"[startup] SRE agent FALLÓ: {exc}", exc_info=True)

    # 4. OTel instrumentation (tracing a otel-collector.observability:4317)
    try:
        from observability.tracing import instrument_app, instrument_requests
        instrument_app(app)
        instrument_requests()
        logger.info("[startup] OpenTelemetry instrumentado")
    except Exception as exc:
        logger.warning(f"[startup] OTel instrumentation falló: {exc}")

    logger.info("=== raphael-service listo en :8002 ===")

    yield  # ← app corriendo

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info("=== raphael-service apagando ===")

    # Drain: permitir que requests in-flight terminen antes de bajar recursos
    import asyncio
    await asyncio.sleep(5)

    try:
        from agents.sre import stop_sre_loop
        stop_sre_loop()
        logger.info("[shutdown] SRE loop detenido")
    except Exception as exc:
        logger.warning(f"[shutdown] SRE loop stop falló: {exc}")

    try:
        from storage.postgres.client import close_pool
        close_pool()
        logger.info("[shutdown] PostgreSQL pool cerrado")
    except Exception as exc:
        logger.warning(f"[shutdown] PostgreSQL close falló: {exc}")

    logger.info("=== raphael-service apagado ===")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Construye y configura la aplicación FastAPI de raphael-service."""
    from config.settings import settings

    app = FastAPI(
        title="raphael-service",
        description=(
            "Microservicio SRE autónomo — absorbe al legacy k8s-agent 5.2.0. "
            "Expone /api/sre/* (loop, incidents, postmortems, SLO, comandos) "
            "y /api/k8s-agent (conversational fallback)."
        ),
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    # ── Observability middleware (request_id, HTTP metrics) ───────────────────
    from observability.middleware import ObservabilityMiddleware
    app.add_middleware(ObservabilityMiddleware)

    # ── Prometheus metrics endpoint ───────────────────────────────────────────
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # ── Health / Readiness (reusa el builder del backend) ─────────────────────
    from observability.health import build_health_router
    health_router = build_health_router()
    if health_router is not None:
        app.include_router(health_router)
    else:
        # Fallback stub — sólo liveness básico sin dependencias externas
        @app.get("/health")
        def _fallback_health():
            return {"status": "ok", "service": "raphael-service"}

        @app.get("/health/{component}")
        def _fallback_component(component: str):
            return {"name": component, "healthy": True, "detail": "stub"}

    # ── Router SRE (re-uso directo del router del monorepo) ───────────────────
    # El router ya implementa: /api/sre/loop/status, /incidents, /postmortems,
    # /learning/stats, /slo/status, /maintenance (GET/POST/DELETE), /command
    from interfaces.api.routers.sre import router as sre_router
    app.include_router(sre_router)

    # ── Endpoints legacy del k8s-agent (conversacional) + deploy hook ─────────
    _register_legacy_endpoints(app)

    return app


# ── Endpoints legacy absorbidos del k8s-agent 5.2.0 ───────────────────────────

class K8sAgentQueryRequest(BaseModel):
    """Body para /api/k8s-agent — conversational query al SRE agent."""
    query: str = Field(..., min_length=1, max_length=4000)
    user_email: str = "unknown"


class DeployHookRequest(BaseModel):
    """Body para /api/sre/deploy-hook — notificación de CI sobre un deploy."""
    service: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=128)
    commit:  str = ""
    author:  str = ""


def _register_legacy_endpoints(app: FastAPI) -> None:
    """
    Registra endpoints que raphael-service hereda del legacy k8s-agent:
      - POST /api/k8s-agent      → delega a agents.sre.query_agent
      - POST /api/sre/deploy-hook → registra deploy reciente en Redis (TTL 1800s)
    """
    from interfaces.api.auth import require_internal_secret

    @app.post("/api/k8s-agent", tags=["legacy"])
    def k8s_agent_query(
        payload: K8sAgentQueryRequest,
        _: Annotated[None, Depends(require_internal_secret)],
    ) -> dict[str, str]:
        """
        Entrypoint conversacional heredado del legacy k8s-agent.
        Reenvía la consulta al `query_agent()` de agents.sre (LangGraph ReAct
        primario + LangChain clásico de fallback + Vault KB fast-path).
        """
        from agents.sre import query_agent

        logger.info(
            f"[legacy.k8s-agent] query user={payload.user_email} "
            f"len={len(payload.query)}"
        )
        try:
            answer = query_agent(payload.query)
            return {
                "answer": answer,
                "user_email": payload.user_email,
                "source": "raphael-service",
            }
        except Exception as exc:
            logger.error(f"[legacy.k8s-agent] query FALLÓ: {exc}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"agent_error: {exc}") from exc

    @app.post("/api/sre/deploy-hook", tags=["sre"])
    def deploy_hook(
        payload: DeployHookRequest,
        _: Annotated[None, Depends(require_internal_secret)],
    ) -> dict[str, str]:
        """
        Notificación de CI cuando un servicio termina de desplegarse.
        Graba en Redis `sre:recent_deploy:{service}` (TTL 1800s) para que
        el SRE agent correlacione anomalías post-deploy y decida auto-rollback.
        """
        try:
            from storage.redis.client import get_client

            client = get_client()
            key = f"sre:recent_deploy:{payload.service}"
            value = (
                f'{{"service":"{payload.service}",'
                f'"version":"{payload.version}",'
                f'"commit":"{payload.commit}",'
                f'"author":"{payload.author}"}}'
            )
            client.set(key, value, ex=1800)
            logger.info(
                f"[deploy-hook] service={payload.service} version={payload.version} "
                f"commit={payload.commit[:8]} author={payload.author}"
            )
            return {
                "status": "recorded",
                "service": payload.service,
                "ttl": "1800",
            }
        except Exception as exc:
            logger.error(f"[deploy-hook] FALLÓ: {exc}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"redis_error: {exc}") from exc


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _run_in_thread(fn) -> None:
    """Ejecuta una función síncrona en un thread para no bloquear el event loop."""
    import asyncio
    await asyncio.to_thread(fn)


# ── App instance (importable sin ejecutar) ────────────────────────────────────

app = create_app()


# ── Dev server ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "raphael_service.main:app",
        host="0.0.0.0",
        port=8002,
        reload=True,
        log_config=None,  # setup_logging() controla todo el logging
    )
