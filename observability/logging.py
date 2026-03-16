"""
Configuración de logging estructurado (JSON) para la plataforma.

En producción (Kubernetes) los logs se emiten en JSON para que
Loki/Grafana puedan indexarlos y hacer queries estructuradas.
En desarrollo se usa formato legible.

Uso:
    from observability.logging import setup_logging, get_logger
    setup_logging()
    logger = get_logger("agents.planner")
    logger.info("Plan generado", extra={"steps": 4, "user_id": "..."})
"""
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ── Context variables para correlación de requests ────────────────────────────
# Se populan en el middleware HTTP y en AgentDispatcher
_request_id_var:     ContextVar[str] = ContextVar("request_id",     default="")
_user_id_var:        ContextVar[str] = ContextVar("user_id",        default="")
_conversation_id_var: ContextVar[str] = ContextVar("conversation_id", default="")


def set_log_context(
    request_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
) -> None:
    """Establece el contexto de correlación para todos los logs del request actual."""
    if request_id:
        _request_id_var.set(request_id)
    if user_id:
        _user_id_var.set(user_id)
    if conversation_id:
        _conversation_id_var.set(conversation_id)


def _get_otel_trace_context() -> Dict[str, str]:
    """
    Extrae trace_id y span_id del span OTel activo.
    Retorna dict vacío si OTel no está disponible o no hay span activo.
    Formato: W3C TraceContext (hex lowercase).
    """
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx  = span.get_span_context()
        if ctx and ctx.is_valid:
            return {
                "trace_id": format(ctx.trace_id, "032x"),
                "span_id":  format(ctx.span_id,  "016x"),
            }
    except Exception:
        pass
    return {}


# Campos internos del LogRecord a excluir del JSON
_EXCLUDED_FIELDS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text",
    "filename", "funcName", "id", "levelname", "levelno",
    "lineno", "module", "msecs", "message", "msg",
    "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName",
    "taskName",
})


class JsonFormatter(logging.Formatter):
    """
    Formatter JSON estructurado con correlación OTel.

    Cada línea de log incluye:
      - timestamp, level, logger, message, service
      - request_id, user_id, conversation_id  (del contexto del request)
      - trace_id, span_id                      (del span OTel activo)
      - cualquier campo extra pasado con extra={...}
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "service":   os.environ.get("OTEL_SERVICE_NAME", "amael"),
        }

        # Correlación de request (via ContextVar)
        if req_id := _request_id_var.get():
            log_entry["request_id"] = req_id
        if user_id := _user_id_var.get():
            log_entry["user_id"] = user_id
        if conv_id := _conversation_id_var.get():
            log_entry["conversation_id"] = conv_id

        # Correlación OTel (trace_id / span_id)
        log_entry.update(_get_otel_trace_context())

        # Campos extra pasados con extra={...}
        for key, value in record.__dict__.items():
            if key not in _EXCLUDED_FIELDS and not key.startswith("_"):
                log_entry[key] = value

        # Información de excepción si existe
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str, ensure_ascii=False)


class ReadableFormatter(logging.Formatter):
    """Formatter legible para desarrollo local."""
    FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self):
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


def setup_logging(
    level: Optional[str] = None,
    json_output: Optional[bool] = None,
) -> None:
    """
    Configura el sistema de logging de la plataforma.

    Args:
        level:       Nivel de log (DEBUG/INFO/WARNING/ERROR). Default: env LOG_LEVEL.
        json_output: True para JSON, False para legible. Default: True en producción.
    """
    log_level = level or os.environ.get("LOG_LEVEL", "INFO")
    environment = os.environ.get("ENVIRONMENT", "production")

    if json_output is None:
        json_output = environment != "development"

    formatter = JsonFormatter() if json_output else ReadableFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

    # Limpiar handlers existentes (evita duplicados en recargas)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Silenciar librerías ruidosas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Retorna un logger nombrado.

    Convención de nombres:
        agent.orchestrator
        agent.sre
        skill.kubernetes
        tool.prometheus
        storage.postgres
        interfaces.api
    """
    return logging.getLogger(name)
