# ── trader-service ───────────────────────────────────────────────────────────
# Agente de trading experimental (Alpaca paper/live). Reutiliza módulos del
# monorepo: agents.trader, config, core, observability, storage, security,
# interfaces.api.auth.
#
# Build desde la raíz del repo:
#   docker build -t registry.richardx.dev/trader-service:1.0.0 \
#                -f trader_service/Dockerfile .
# -----------------------------------------------------------------------------

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY trader_service/requirements.txt ./requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --shell /bin/bash trader

WORKDIR /app

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# ── Copy selectivo — solo lo que trader-service importa ──────────────────────
COPY --chown=trader:trader agents/__init__.py     /app/agents/__init__.py
COPY --chown=trader:trader agents/trader/         /app/agents/trader/
COPY --chown=trader:trader core/                  /app/core/
COPY --chown=trader:trader observability/         /app/observability/
COPY --chown=trader:trader storage/               /app/storage/
COPY --chown=trader:trader config/                /app/config/
COPY --chown=trader:trader security/              /app/security/
COPY --chown=trader:trader interfaces/__init__.py         /app/interfaces/__init__.py
COPY --chown=trader:trader interfaces/api/__init__.py     /app/interfaces/api/__init__.py
COPY --chown=trader:trader interfaces/api/auth.py         /app/interfaces/api/auth.py
COPY --chown=trader:trader interfaces/api/middleware/     /app/interfaces/api/middleware/
COPY --chown=trader:trader trader_service/        /app/trader_service/

USER trader

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8003/health || exit 1

# 1 worker: APScheduler necesita un único proceso (si no, el loop correría
# N veces y duplicaría órdenes).
CMD ["uvicorn", "trader_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8003", \
     "--workers", "1", \
     "--no-access-log"]
