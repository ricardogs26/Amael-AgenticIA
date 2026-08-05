# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps:
#   gcc         — extensiones C (grpcio, numpy)
#   libffi-dev  — cryptography (python-jose)
#   libssl-dev  — cryptography / ssl
#   libpq-dev   — psycopg2 (aunque usamos la versión binary, alguna dep transitiva puede necesitarlo)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Usar venv para evitar conflictos con --prefix y paquetes con scripts post-install
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Copiar solo pyproject.toml primero para cachear la capa de dependencias
COPY pyproject.toml .

# setuptools.packages.find escanea el filesystem; crear stubs mínimos para que
# pueda resolver la metadata del proyecto sin necesitar el código real.
RUN mkdir -p core config observability security storage \
             agents skills tools orchestration llm memory interfaces && \
    for d in core config observability security storage \
              agents skills tools orchestration llm memory interfaces; do \
      touch $d/__init__.py; \
    done

# Instalar dependencias base + extras SRE + docs en el venv
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir ".[sre,docs]"


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime deps:
#   libmagic1 — python-magic
#   libpq5    — psycopg2-binary en runtime
#   ffmpeg    — conversión WAV→OGG OPUS para notas de voz WhatsApp
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    libpq5 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root
RUN useradd --uid 1000 --create-home --shell /bin/bash amael

WORKDIR /app

# Copiar venv completo desde el builder
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# Copiar código fuente
COPY --chown=amael:amael . .

USER amael

# Entrypoint por servicio. El repo publica varias imágenes desde este mismo
# Dockerfile y cada una arranca un módulo y un puerto distintos:
#
#   amael-agentic-backend  main:app                    8000  (defaults)
#   raphael-service        raphael_service.main:app    8002
#   camael-service         camael_service.main:app     8003
#   trader-service         trader_service.main:app     8003
#
# Antes solo existían los defaults del backend y las demás imágenes se
# construían con un CMD override fuera de git — el repo no podía reproducirlas.
# Se descubrió al reconstruir raphael 1.1.11, que salió arrancando `main:app`
# en 8000 mientras el Deployment sondeaba 8002 → liveness en connection refused.
ARG APP_MODULE=main:app
ARG APP_PORT=8000
ENV APP_MODULE=${APP_MODULE} \
    APP_PORT=${APP_PORT}

EXPOSE ${APP_PORT}

# Health check que usa el endpoint /health de la propia app
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ['APP_PORT'])" || exit 1

# 1 worker: FastAPI es async y maneja concurrencia internamente.
# Con 2 workers, APScheduler corre en 2 procesos → doble SRE loop → RFCs duplicados.
# Shell form + exec: uvicorn queda como PID 1 y recibe SIGTERM directo.
CMD exec uvicorn "$APP_MODULE" \
      --host 0.0.0.0 \
      --port "$APP_PORT" \
      --workers 1 \
      --no-access-log
