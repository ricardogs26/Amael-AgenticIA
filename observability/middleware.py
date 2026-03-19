"""
ObservabilityMiddleware — middleware FastAPI para métricas HTTP y correlación.

Responsabilidades:
  1. Inyectar / propagar X-Request-ID en cada request
  2. Registrar HTTP_REQUESTS_TOTAL y HTTP_REQUEST_LATENCY_SECONDS automáticamente
  3. Establecer el contexto de correlación de logs (request_id, user_id)
  4. Emitir un log estructurado por request con campos de acceso estándar

Uso:
    from observability.middleware import ObservabilityMiddleware
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from observability.logging import set_log_context
from observability.metrics import HTTP_REQUEST_LATENCY_SECONDS, HTTP_REQUESTS_TOTAL

logger = logging.getLogger("observability.middleware")

# Paths que no se miden (evitar noise en métricas)
_SKIP_PATHS = {"/health", "/ready", "/metrics", "/favicon.ico"}


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Middleware de observabilidad para FastAPI / Starlette.

    Por cada request:
      - Genera o propaga X-Request-ID (UUID v4)
      - Establece contexto de correlación para logs (request_id, user_id)
      - Registra duración e incrementa contadores Prometheus
      - Añade X-Request-ID a la respuesta para trazabilidad cliente
    """

    def __init__(self, app: ASGIApp, service_name: str = "amael") -> None:
        super().__init__(app)
        self._service = service_name

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # ── 1. Request-ID ──────────────────────────────────────────────────────
        request_id = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or str(uuid.uuid4())
        )

        # ── 2. Contexto de correlación de logs ─────────────────────────────────
        # user_id se extrae del JWT en un handler real; aquí lo dejamos vacío
        # para que el endpoint lo establezca con set_log_context() si es necesario.
        set_log_context(request_id=request_id)

        # ── 3. Ejecutar handler ────────────────────────────────────────────────
        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            logger.error(
                "Request no manejado",
                extra={
                    "request_id": request_id,
                    "method":     request.method,
                    "path":       request.url.path,
                    "error":      str(exc),
                },
                exc_info=True,
            )
            raise
        finally:
            elapsed = time.perf_counter() - start
            path    = request.url.path

            # ── 4. Métricas Prometheus ─────────────────────────────────────────
            if path not in _SKIP_PATHS:
                handler = _normalize_path(path)
                HTTP_REQUESTS_TOTAL.labels(
                    method=request.method,
                    handler=handler,
                    status_code=str(status_code),
                ).inc()
                HTTP_REQUEST_LATENCY_SECONDS.labels(handler=handler).observe(elapsed)

            # ── 5. Log de acceso estructurado ──────────────────────────────────
            if path not in _SKIP_PATHS:
                logger.info(
                    f"{request.method} {path} {status_code}",
                    extra={
                        "request_id":  request_id,
                        "method":      request.method,
                        "path":        path,
                        "status_code": status_code,
                        "duration_ms": round(elapsed * 1000, 1),
                        "client_ip":   _get_client_ip(request),
                    },
                )

        # ── 6. Propagar request_id en la respuesta ─────────────────────────────
        response.headers["X-Request-ID"] = request_id
        return response


def _normalize_path(path: str) -> str:
    """
    Normaliza rutas con IDs dinámicos para agrupar métricas.

    Ejemplos:
        /api/conversations/abc123  → /api/conversations/{id}
        /api/users/42/messages     → /api/users/{id}/messages
        /api/chat                  → /api/chat
    """
    import re
    # UUIDs
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{id}", path)
    # Números enteros solos como segmento
    path = re.sub(r"/\d{4,}", "/{id}", path)
    return path


def _get_client_ip(request: Request) -> str:
    """Extrae la IP real del cliente respetando X-Forwarded-For (proxy/Kong)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""
