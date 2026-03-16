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
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    libpq5 \
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

# Puerto de la API
EXPOSE 8000

# Health check que usa el endpoint /health de la propia app
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Uvicorn con 2 workers — log-config /dev/null para que setup_logging() controle JSON
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--no-access-log"]
