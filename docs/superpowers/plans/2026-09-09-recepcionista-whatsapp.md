# Recepcionista de WhatsApp — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un número desconocido que escribe al bridge conversa con un recepcionista limitado que captura nombre/empresa/motivo y avisa a Ricardo; Ricardo gestiona con `/lead`.

**Architecture:** Módulo `agents/reception/` (storage + receptionist + commands) y router `/api/reception/*` protegido con el secreto interno. El bridge delega en el backend todo mensaje de número no registrado. Guardarraíles en Redis y código; el LLM solo redacta y extrae campos con `format=json`.

**Tech Stack:** FastAPI, psycopg2 (`storage.postgres.client.get_connection`), Redis (`storage.redis.client.get_client`), `langchain_ollama.ChatOllama`, prometheus_client, pytest (monkeypatch, sin DB real).

**Spec:** `docs/superpowers/specs/2026-09-09-recepcionista-whatsapp-design.md`

## Global Constraints

- Auth de los endpoints: `Depends(require_internal_secret)` (`Authorization: Bearer INTERNAL_API_SECRET`).
- LLM: `settings.llm_model_fast`, `reasoning=False`, `format="json"`, timeout 45 s, sin herramientas.
- Límites: `RECEPTION_MAX_PER_PHONE=15` (24 h), `RECEPTION_MAX_PER_DAY=100`, texto ≤ 500 chars, contexto 8 turnos, reply ≤ 1000 chars.
- Prompt sin citar lo prohibido (1.17.2).
- El bump de versión del manifest va en el mismo push a main (regla del CI).

---

### Task 1: storage de leads
**Files:** Create `agents/reception/__init__.py`, `agents/reception/storage.py`; Test `tests/unit/agents/reception/test_storage.py`
**Produces:** `Lead` dataclass (`id, phone, name, company, reason, status, message_count, notified_at`), `ensure_schema()`, `get_or_create(phone) -> Lead`, `get(lead_id) -> Lead|None`, `list_open(limit=10) -> list[Lead]`, `update_fields(lead_id, **campos)`, `add_message(lead_id, role, content)`, `recent_messages(lead_id, n=8) -> list[tuple[role, content]]`, `set_status(lead_id, status)`, `bump_count(lead_id) -> int`, `mark_notified(lead_id)`.
- [ ] Tests: `update_fields` ignora valores vacíos y no pisa existentes (prueba de la función pura `merge_fields(lead, extracted) -> dict`).
- [ ] Implementar, correr, commit.

### Task 2: receptionist
**Files:** Create `agents/reception/profile_public.md`, `agents/reception/prompts.py`, `agents/reception/receptionist.py`; Modify `observability/metrics.py` (+2 métricas), `config/settings.py` (+2 campos); Test `tests/unit/agents/reception/test_receptionist.py`
**Produces:** `handle_message(phone, text, has_media=False) -> str|None`; funciones puras `parse_llm_json(raw) -> dict`, `should_notify(lead, completed_now) -> str|None` (`"first"|"summary"|"forced"|None`), `check_limits(phone) -> str|None`.
- [ ] Tests: JSON parcial/inválido/reply largo; límites por número y global (Redis fake); silenciado → None sin LLM; media → texto fijo; avisos first/summary(5)/forced(10).
- [ ] Implementar, correr, commit.

### Task 3: comandos /lead
**Files:** Create `agents/reception/commands.py`; Test `tests/unit/agents/reception/test_commands.py`
**Produces:** `dispatch(command: str, phone: str) -> str`, `approve_lead(lead)` (reusa SQL de `redeem_pair_code`), `is_admin_phone(phone) -> bool`.
- [ ] Tests: no admin → «Comando no disponible»; parse de `<n> aprobar|rechazar|responder <texto>`; responder sin texto → error corto.
- [ ] Implementar, correr, commit.

### Task 4: router + main
**Files:** Create `interfaces/api/routers/reception.py`; Modify `main.py` (include_router + `ensure_schema()` en `_ensure_schema`); Test `tests/unit/api/test_reception_router.py`
- [ ] Tests: rutas presentes en `app.openapi()`; sin secreto → 403.
- [ ] Commit.

### Task 5: bridge
**Files:** Modify `../Amael-IA/whatsapp-bridge/index.js` (rama `!access.allowed`, ruteo `/lead`, `GET /me`).
- [ ] `node --check`; actualizar ConfigMap `whatsapp-bridge-code`; `rollout restart`; leer `GET /me`.

### Task 6: despliegue backend
- [ ] `k8s/config/01-configmap.yaml` (+RECEPTION_*), bump `05-backend-deployment.yaml` a 1.18.0, ruff + pytest, push a main, esperar CI, `kubectl rollout status`.

### Task 7: sitio
- [ ] Botón WhatsApp en `GitOps-Infra/profile-site/index.html`, commit y push a `develop`.

### Task 8: cierre
- [ ] E2E manual, CLAUDE.md (tabla de versiones), memoria, nota del vault.
