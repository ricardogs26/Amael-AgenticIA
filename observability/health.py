"""
health — Endpoints de liveness y readiness para Amael-AgenticIA.

Liveness  (/health): ¿Está vivo el proceso? → siempre True si la app arrancó.
Readiness (/ready):  ¿Puede procesar requests? → verifica storage + skills + tools.

Uso con FastAPI:
    from observability.health import build_health_router
    app.include_router(build_health_router())

Uso standalone:
    from observability.health import liveness, readiness
    result = await readiness()
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("observability.health")


# ── Modelos de respuesta ──────────────────────────────────────────────────────

class ComponentHealth(BaseModel):
    name:    str
    healthy: bool
    latency_ms: float = 0.0
    detail: str = ""


class HealthResponse(BaseModel):
    status:     str              # "ok" | "degraded" | "unavailable"
    version:    str
    uptime_s:   float
    components: dict[str, Any] = {}


_START_TIME = time.monotonic()
_VERSION    = os.environ.get("APP_VERSION", "dev")


# ── Liveness ──────────────────────────────────────────────────────────────────

def liveness() -> dict[str, str]:
    """
    Liveness check: el proceso está vivo y la app arrancó.
    No verifica dependencias externas — sólo que el proceso responde.
    Kubernetes usa esto para decidir si reiniciar el pod.
    """
    return {"status": "ok", "version": _VERSION}


# ── Readiness ─────────────────────────────────────────────────────────────────

async def readiness() -> HealthResponse:
    """
    Readiness check completo: verifica storage, skills y tools en paralelo.
    Kubernetes usa esto para decidir si enviar tráfico al pod.

    Reglas de degradación:
      - Storage (postgres/redis) unhealthy → status = "unavailable"
      - Skills/Tools unhealthy             → status = "degraded"
      - Todo ok                            → status = "ok"
    """
    checks = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_qdrant(),
        _check_ollama(),
        _check_skills(),
        _check_tools(),
        return_exceptions=True,
    )

    postgres_result, redis_result, qdrant_result, ollama_result, skills_results, tools_results = checks

    components: dict[str, Any] = {}
    all_storage_ok = True
    any_skill_fail = False
    any_tool_fail  = False

    # Storage
    for label, result in [
        ("postgres", postgres_result),
        ("redis",    redis_result),
        ("qdrant",   qdrant_result),
        ("ollama",   ollama_result),
    ]:
        if isinstance(result, Exception):
            components[label] = ComponentHealth(name=label, healthy=False, detail=str(result))
            all_storage_ok = False
        else:
            components[label] = result
            if not result.healthy:
                all_storage_ok = False

    # Skills
    if isinstance(skills_results, Exception):
        components["skills"] = {"error": str(skills_results)}
    else:
        for name, comp in skills_results.items():
            components[f"skill.{name}"] = comp
            if not comp.healthy:
                any_skill_fail = True

    # Tools
    if isinstance(tools_results, Exception):
        components["tools"] = {"error": str(tools_results)}
    else:
        for name, comp in tools_results.items():
            components[f"tool.{name}"] = comp
            if not comp.healthy:
                any_tool_fail = True

    # Estado global
    if not all_storage_ok:
        status = "unavailable"
    elif any_skill_fail or any_tool_fail:
        status = "degraded"
    else:
        status = "ok"

    # Actualizar gauge de métricas
    _update_health_metrics(components)

    return HealthResponse(
        status=status,
        version=_VERSION,
        uptime_s=round(time.monotonic() - _START_TIME, 1),
        components={k: v.model_dump() if hasattr(v, "model_dump") else v
                    for k, v in components.items()},
    )


# ── Checks individuales ───────────────────────────────────────────────────────

async def _check_postgres() -> ComponentHealth:
    t0 = time.monotonic()
    try:
        from storage.postgres.client import health_check as pg_health
        healthy = await asyncio.to_thread(pg_health)
        return ComponentHealth(
            name="postgres",
            healthy=healthy,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail="" if healthy else "SELECT 1 failed",
        )
    except Exception as exc:
        logger.warning(f"[health] postgres check failed: {exc}")
        return ComponentHealth(
            name="postgres",
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail=str(exc),
        )


async def _check_redis() -> ComponentHealth:
    t0 = time.monotonic()
    try:
        from storage.redis.client import health_check as redis_health
        healthy = await asyncio.to_thread(redis_health)
        return ComponentHealth(
            name="redis",
            healthy=healthy,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail="" if healthy else "PING failed",
        )
    except Exception as exc:
        logger.warning(f"[health] redis check failed: {exc}")
        return ComponentHealth(
            name="redis",
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail=str(exc),
        )


async def _check_qdrant() -> ComponentHealth:
    t0 = time.monotonic()
    url = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
    try:
        def _ping():
            urllib.request.urlopen(f"{url}/healthz", timeout=3)
        await asyncio.to_thread(_ping)
        return ComponentHealth(
            name="qdrant",
            healthy=True,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
        )
    except Exception as exc:
        logger.warning(f"[health] qdrant check failed: {exc}")
        return ComponentHealth(
            name="qdrant",
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail=str(exc),
        )


async def _check_ollama() -> ComponentHealth:
    t0 = time.monotonic()
    url = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
    try:
        def _ping():
            urllib.request.urlopen(f"{url}/api/tags", timeout=5)
        await asyncio.to_thread(_ping)
        return ComponentHealth(
            name="ollama",
            healthy=True,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
        )
    except Exception as exc:
        logger.warning(f"[health] ollama check failed: {exc}")
        return ComponentHealth(
            name="ollama",
            healthy=False,
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            detail=str(exc),
        )


async def _check_skills() -> dict[str, ComponentHealth]:
    """Ejecuta health_check() en todas las skills registradas."""
    results: dict[str, ComponentHealth] = {}
    try:
        from skills.registry import SkillRegistry
        for name in SkillRegistry.names():
            t0 = time.monotonic()
            try:
                skill   = SkillRegistry.get(name)
                healthy = await skill.health_check()
                results[name] = ComponentHealth(
                    name=name,
                    healthy=healthy,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                )
            except Exception as exc:
                results[name] = ComponentHealth(
                    name=name,
                    healthy=False,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                    detail=str(exc),
                )
    except ImportError:
        pass
    return results


async def _check_tools() -> dict[str, ComponentHealth]:
    """Ejecuta health_check() en todas las tools registradas."""
    results: dict[str, ComponentHealth] = {}
    try:
        from tools.registry import ToolRegistry
        for name in ToolRegistry.names():
            t0 = time.monotonic()
            try:
                tool    = ToolRegistry.get(name)
                healthy = await tool.health_check()
                results[name] = ComponentHealth(
                    name=name,
                    healthy=healthy,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                )
            except Exception as exc:
                results[name] = ComponentHealth(
                    name=name,
                    healthy=False,
                    latency_ms=round((time.monotonic() - t0) * 1000, 1),
                    detail=str(exc),
                )
    except ImportError:
        pass
    return results


def _update_health_metrics(components: dict[str, Any]) -> None:
    """Actualiza el Gauge REGISTRY_HEALTH_STATUS con el estado de cada componente."""
    try:
        from observability.metrics import REGISTRY_HEALTH_STATUS
        for key, comp in components.items():
            if hasattr(comp, "healthy"):
                ctype, _, cname = key.partition(".")
                if not cname:
                    ctype, cname = "storage", key
                REGISTRY_HEALTH_STATUS.labels(
                    component_type=ctype,
                    component_name=cname,
                ).set(1 if comp.healthy else 0)
    except Exception:
        pass


# ── FastAPI router helper ─────────────────────────────────────────────────────

def build_health_router():
    """
    Construye un APIRouter con /health y /ready.
    Importa FastAPI sólo si está disponible (evita dependencia en tests).

    Uso:
        app.include_router(build_health_router())
    """
    try:
        from fastapi import APIRouter
        from fastapi.responses import JSONResponse

        router = APIRouter(tags=["health"])

        @router.get("/health")
        def health_endpoint():
            return liveness()

        @router.get("/ready")
        async def ready_endpoint():
            result = await readiness()
            status_code = 200 if result.status in ("ok", "degraded") else 503
            return JSONResponse(content=result.model_dump(), status_code=status_code)

        return router

    except ImportError:
        logger.warning("[health] FastAPI no disponible. build_health_router() retorna None.")
        return None
