"""
main.py — Entry point de Amael-AgenticIA.

Secuencia de arranque:
  1. Logging estructurado (JSON en prod, legible en dev)
  2. PostgreSQL pool
  3. Redis client
  4. Skills registry (KubernetesSkill, RAGSkill, LLMSkill, VaultSkill, WebSkill)
  5. Tools registry (PrometheusTool, GrafanaTool, WhatsAppTool, GitHubTool)
  6. SRE: init_sre_db, init_runbooks_qdrant, start_sre_loop
  7. LangGraph orchestrator pre-compilado (warm-up)
  8. FastAPI app + middleware + routers

Arranque:
    uvicorn main:app --host 0.0.0.0 --port 8000

En Kubernetes usa el Deployment k8s/02.-backend-deployment.yaml
con imagen registry.richardx.dev/backend-ia:<version>.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from starlette.middleware.sessions import SessionMiddleware

# ── Logging primero — antes de cualquier otro import ──────────────────────────
from observability.logging import setup_logging
setup_logging()

logger = logging.getLogger("main")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup / shutdown de la aplicación.
    AsyncContextManager: todo lo que está antes del yield es startup,
    todo lo que está después es shutdown.
    """
    # ── STARTUP ───────────────────────────────────────────────────────────────
    logger.info("=== Amael-AgenticIA iniciando ===")

    from config.settings import settings

    # 1. PostgreSQL
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

    # 3. Skills registry
    try:
        from skills.registry import register_all_skills
        register_all_skills()
        logger.info("[startup] Skills registradas")
    except Exception as exc:
        logger.error(f"[startup] Skills registry FALLÓ: {exc}", exc_info=True)

    # 4. Tools registry
    try:
        from tools.registry import register_all_tools
        register_all_tools()
        logger.info("[startup] Tools registradas")
    except Exception as exc:
        logger.error(f"[startup] Tools registry FALLÓ: {exc}", exc_info=True)

    # 4b. Agents registry
    try:
        from agents.base.agent_registry import register_all_agents
        register_all_agents()
        logger.info("[startup] Agentes registrados")
    except Exception as exc:
        logger.error(f"[startup] Agents registry FALLÓ: {exc}", exc_info=True)

    # 5. Schema PostgreSQL (conversations + messages)
    try:
        _ensure_schema()
        logger.info("[startup] Schema PostgreSQL verificado")
    except Exception as exc:
        logger.warning(f"[startup] Schema check falló: {exc}")

    # 6. SRE agent
    try:
        from agents.sre import init_sre_db, init_runbooks_qdrant, start_sre_loop
        init_sre_db()
        await _run_in_thread(init_runbooks_qdrant)
        start_sre_loop()
        logger.info("[startup] SRE agent iniciado")
    except Exception as exc:
        logger.error(f"[startup] SRE agent FALLÓ: {exc}", exc_info=True)

    # 7. LangGraph warm-up (compilar grafo una vez antes del primer request)
    try:
        from orchestration.workflow_engine import get_orchestrator
        get_orchestrator()
        logger.info("[startup] LangGraph orchestrator compilado")
    except Exception as exc:
        logger.warning(f"[startup] LangGraph warm-up falló: {exc}")

    # 8. OTel instrumentation
    try:
        from observability.tracing import instrument_app, instrument_requests
        instrument_app(app)
        instrument_requests()
        logger.info("[startup] OpenTelemetry instrumentado")
    except Exception as exc:
        logger.warning(f"[startup] OTel instrumentation falló: {exc}")

    logger.info("=== Amael-AgenticIA listo para recibir requests ===")

    yield  # ←── app corriendo

    # ── SHUTDOWN ──────────────────────────────────────────────────────────────
    logger.info("=== Amael-AgenticIA apagando ===")

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

    logger.info("=== Amael-AgenticIA apagado ===")


# ── App ───────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Construye y configura la aplicación FastAPI."""
    from config.settings import settings

    app = FastAPI(
        title="Amael-AgenticIA",
        description="Plataforma multi-agente modular para automatización inteligente",
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    # ── Session (requerido por authlib OAuth state) ───────────────────────────
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key,
        https_only=True,
        same_site="lax",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://amael-ia.richardx.dev",
            "http://localhost:3000",
            "http://localhost:8501",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Observability middleware ───────────────────────────────────────────────
    from observability.middleware import ObservabilityMiddleware
    app.add_middleware(ObservabilityMiddleware)

    # ── Prometheus metrics endpoint ───────────────────────────────────────────
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # ── Health / Readiness ────────────────────────────────────────────────────
    from observability.health import build_health_router
    health_router = build_health_router()
    if health_router:
        app.include_router(health_router)

    # ── API routers ───────────────────────────────────────────────────────────
    from interfaces.api.routers.chat          import router as chat_router
    from interfaces.api.routers.conversations import router as conv_router
    from interfaces.api.routers.identity      import router as identity_router
    from interfaces.api.routers.planner       import router as planner_router
    from interfaces.api.routers.sre           import router as sre_router
    from interfaces.api.routers.feedback      import router as feedback_router
    from interfaces.api.routers.auth          import router as auth_router
    from interfaces.api.routers.profile       import router as profile_router
    from interfaces.api.routers.admin         import router as admin_router
    from interfaces.api.routers.ingest        import router as ingest_router
    from interfaces.api.routers.documents     import router as documents_router
    from interfaces.api.routers.tasks         import router as tasks_router
    from interfaces.api.routers.memory        import router as memory_router

    app.include_router(chat_router)
    app.include_router(conv_router)
    app.include_router(identity_router)
    app.include_router(planner_router)
    app.include_router(sre_router)
    app.include_router(feedback_router)
    app.include_router(auth_router)
    app.include_router(profile_router)
    app.include_router(admin_router)
    app.include_router(ingest_router)
    app.include_router(documents_router)
    app.include_router(tasks_router)        # POST /api/agent/task — Phase 1
    app.include_router(memory_router)       # GET/DELETE /api/memory — Phase 8

    return app


app = create_app()


# ── Schema helpers ────────────────────────────────────────────────────────────

def _ensure_schema() -> None:
    """
    Crea las tablas base si no existen.
    Idempotente — seguro de llamar en cada arranque.
    """
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            # ── Conversaciones y mensajes ──────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id          TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    title       TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations (user_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id               TEXT PRIMARY KEY,
                    conversation_id  TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    role             TEXT NOT NULL,
                    content          TEXT,
                    intent           TEXT,
                    created_at       TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages (conversation_id, created_at ASC)
            """)

            # ── Usuarios — fuente de verdad para control de acceso ─────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id      TEXT PRIMARY KEY,
                    display_name TEXT,
                    role         TEXT NOT NULL DEFAULT 'user',
                    status       TEXT NOT NULL DEFAULT 'active',
                    timezone     TEXT DEFAULT 'America/Mexico_City',
                    preferences  JSONB DEFAULT '{}',
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_identities (
                    id                SERIAL PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL REFERENCES user_profile(user_id) ON DELETE CASCADE,
                    identity_type     TEXT NOT NULL,
                    identity_value    TEXT NOT NULL,
                    created_at        TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (identity_type, identity_value)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_identities_value
                ON user_identities (identity_value)
            """)

            # ── Configuración de la plataforma ─────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS platform_settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)


async def _run_in_thread(fn) -> None:
    """Ejecuta una función síncrona en un thread para no bloquear el event loop."""
    import asyncio
    await asyncio.to_thread(fn)


# ── Dev server ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=None,   # deshabilita log_config para que setup_logging() controle todo
    )
