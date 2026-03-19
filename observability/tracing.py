"""
Configuración de OpenTelemetry para toda la plataforma Amael-AgenticIA.

Exporta un tracer pre-configurado que envía spans al OTel Collector
en el namespace observability (otel-collector:4317).

Uso:
    from observability.tracing import tracer, instrument_app
    with tracer.start_as_current_span("agent.planner") as span:
        span.set_attribute("agent.question_length", len(question))
"""
import logging
import os

logger = logging.getLogger(__name__)

OTEL_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-collector.observability.svc.cluster.local:4317",
)
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "amael-backend")

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _resource = Resource.create({"service.name": SERVICE_NAME})
    _provider = TracerProvider(resource=_resource)
    _exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    _provider.add_span_processor(BatchSpanProcessor(_exporter))
    trace.set_tracer_provider(_provider)

    tracer = trace.get_tracer(SERVICE_NAME)
    _otel_available = True
    logger.info(f"[TRACING] OpenTelemetry configurado → {OTEL_ENDPOINT} (servicio: {SERVICE_NAME})")

except ImportError:
    import contextlib

    class _NoOpSpan:
        def set_attribute(self, *a, **kw): pass
        def record_exception(self, *a, **kw): pass
        def set_status(self, *a, **kw): pass
        def add_event(self, *a, **kw): pass

    class _NoOpTracer:
        @contextlib.contextmanager
        def start_as_current_span(self, name, **kw):
            yield _NoOpSpan()

    tracer = _NoOpTracer()
    _otel_available = False
    logger.warning("[TRACING] opentelemetry-sdk no instalado. Usando tracer no-op.")


def instrument_app(app) -> None:
    """
    Instrumenta una aplicación FastAPI con OpenTelemetry.
    Llamar una vez al inicio, después de crear la app.

    Ejemplo:
        app = FastAPI()
        instrument_app(app)
    """
    if not _otel_available:
        logger.warning("[TRACING] instrument_app() ignorado: opentelemetry no disponible.")
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        logger.info("[TRACING] FastAPI y HTTPX instrumentados con OpenTelemetry.")
    except Exception as e:
        logger.warning(f"[TRACING] Error al instrumentar app: {e}")


def instrument_requests() -> None:
    """Instrumenta la librería requests (usado por k8s-agent)."""
    if not _otel_available:
        return
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
        logger.info("[TRACING] requests instrumentado con OpenTelemetry.")
    except Exception as e:
        logger.warning(f"[TRACING] Error al instrumentar requests: {e}")
