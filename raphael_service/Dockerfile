# ── raphael-service ──────────────────────────────────────────────────────────
# Microservicio SRE autónomo. Reutiliza módulos del monorepo Amael-AgenticIA
# (agents.sre, core, observability, storage, skills.{llm,rag,vault}, clients,
#  interfaces.api.auth, interfaces.api.routers.sre, security, config).
#
# Build desde la raíz del repo (no desde raphael_service/):
#   docker build -t registry.richardx.dev/raphael-service:1.0.0 \
#                -f raphael_service/Dockerfile .
# -----------------------------------------------------------------------------

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps (gcc para compilar wheels C que no tengan binarios precompilados
# para Python 3.12 — e.g. psycopg2-binary ya los tiene, pero grpcio a veces no).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY raphael_service/requirements.txt ./requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Runtime deps:
#   libpq5   — psycopg2-binary
#   curl     — HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root (UID 1000)
RUN useradd --uid 1000 --create-home --shell /bin/bash raphael

WORKDIR /app

# Venv
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# ── Copy selectivo del código fuente ─────────────────────────────────────────
# Copiamos únicamente lo que raphael-service necesita para mantener la imagen
# pequeña. El PYTHONPATH=/app permite que `from agents.sre import ...`,
# `from interfaces.api.routers.sre import router`, etc., resuelvan.
COPY --chown=raphael:raphael agents/              /app/agents/
COPY --chown=raphael:raphael core/                /app/core/
COPY --chown=raphael:raphael observability/       /app/observability/
COPY --chown=raphael:raphael storage/             /app/storage/
COPY --chown=raphael:raphael skills/__init__.py   /app/skills/__init__.py
COPY --chown=raphael:raphael skills/base/         /app/skills/base/
COPY --chown=raphael:raphael skills/llm/          /app/skills/llm/
COPY --chown=raphael:raphael skills/rag/          /app/skills/rag/
COPY --chown=raphael:raphael skills/vault/        /app/skills/vault/
COPY --chown=raphael:raphael config/              /app/config/
COPY --chown=raphael:raphael clients/             /app/clients/
COPY --chown=raphael:raphael security/            /app/security/
COPY --chown=raphael:raphael interfaces/__init__.py              /app/interfaces/__init__.py
COPY --chown=raphael:raphael interfaces/api/__init__.py          /app/interfaces/api/__init__.py
COPY --chown=raphael:raphael interfaces/api/auth.py              /app/interfaces/api/auth.py
COPY --chown=raphael:raphael interfaces/api/middleware/          /app/interfaces/api/middleware/
COPY --chown=raphael:raphael interfaces/api/routers/__init__.py  /app/interfaces/api/routers/__init__.py
COPY --chown=raphael:raphael interfaces/api/routers/sre.py       /app/interfaces/api/routers/sre.py
COPY --chown=raphael:raphael runbooks/            /app/runbooks/
COPY --chown=raphael:raphael raphael_service/     /app/raphael_service/

USER raphael

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8002/health || exit 1

# 1 worker: APScheduler necesita un único proceso (de lo contrario el loop
# SRE correría N veces y duplicaría acciones/RFCs).
CMD ["uvicorn", "raphael_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8002", \
     "--workers", "1", \
     "--no-access-log"]
