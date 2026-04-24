# ── camael-service ───────────────────────────────────────────────────────────
# Microservicio DevOps/GitOps autónomo. Reutiliza módulos del monorepo
# Amael-AgenticIA (agents.devops, agents.base, agents.sre.bug_library, core,
# observability, storage, skills.{llm,rag}, clients, interfaces.api.auth,
# interfaces.api.routers.{camael,devops}, security, config).
#
# Build desde la raíz del repo (no desde camael_service/):
#   docker build -t registry.richardx.dev/camael-service:1.0.0 \
#                -f camael_service/Dockerfile .
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

COPY camael_service/requirements.txt ./requirements.txt
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
RUN useradd --uid 1000 --create-home --shell /bin/bash camael

WORKDIR /app

# Venv
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# ── Copy selectivo del código fuente ─────────────────────────────────────────
# Copiamos únicamente lo que camael-service necesita para mantener la imagen
# pequeña. El PYTHONPATH=/app permite que `from agents.devops import ...`,
# `from interfaces.api.routers.camael import router`, etc., resuelvan.
#
# Nota: incluimos también raphael_service/ para mantener la convención monorepo
# (misma imagen base podría soportar ambos entrypoints en teoría, pero el CMD
# fija camael_service.main:app).
COPY --chown=camael:camael agents/              /app/agents/
COPY --chown=camael:camael core/                /app/core/
COPY --chown=camael:camael observability/       /app/observability/
COPY --chown=camael:camael storage/             /app/storage/
COPY --chown=camael:camael skills/__init__.py   /app/skills/__init__.py
COPY --chown=camael:camael skills/base/         /app/skills/base/
COPY --chown=camael:camael skills/llm/          /app/skills/llm/
COPY --chown=camael:camael skills/rag/          /app/skills/rag/
COPY --chown=camael:camael skills/vault/        /app/skills/vault/
COPY --chown=camael:camael config/              /app/config/
COPY --chown=camael:camael clients/             /app/clients/
COPY --chown=camael:camael security/            /app/security/
COPY --chown=camael:camael interfaces/__init__.py              /app/interfaces/__init__.py
COPY --chown=camael:camael interfaces/api/__init__.py          /app/interfaces/api/__init__.py
COPY --chown=camael:camael interfaces/api/auth.py              /app/interfaces/api/auth.py
COPY --chown=camael:camael interfaces/api/middleware/          /app/interfaces/api/middleware/
COPY --chown=camael:camael interfaces/api/routers/camael.py    /app/interfaces/api/routers/camael.py
COPY --chown=camael:camael interfaces/api/routers/devops.py    /app/interfaces/api/routers/devops.py
# El __init__.py del repo importa TODOS los routers (chat, conversations, etc.)
# que camael-service NO necesita. Sobrescribimos con uno vacío que solo
# marca el paquete como importable — `from interfaces.api.routers.camael import router`
# sigue funcionando porque Python resuelve el submódulo directamente.
RUN echo '"""camael-service stub — solo routers camael y devops disponibles."""' \
    > /app/interfaces/api/routers/__init__.py \
    && chown camael:camael /app/interfaces/api/routers/__init__.py
COPY --chown=camael:camael runbooks/            /app/runbooks/
COPY --chown=camael:camael raphael_service/     /app/raphael_service/
COPY --chown=camael:camael camael_service/      /app/camael_service/

USER camael

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8003/health || exit 1

# 1 worker: APScheduler (drain WAL cada 5min) necesita un único proceso
# (de lo contrario el drain correría N veces y duplicaría RFC updates).
CMD ["uvicorn", "camael_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8003", \
     "--workers", "1", \
     "--no-access-log"]
