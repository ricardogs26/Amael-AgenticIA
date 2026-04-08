# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Context

**Amael-AgenticIA** (v1.4.5) is the modular successor to `Amael-IA/backend-ia`. It implements the same LangGraph multi-agent pipeline (Planner → Grouper → Executor → Supervisor) but with clean separation between agents, skills, tools, and orchestration layers. It runs as `amael-agentic-backend` in the `amael-ia` Kubernetes namespace.

The parent repo's `CLAUDE.md` at `/home/richardx/k8s-lab/CLAUDE.md` covers cluster-wide architecture, image versioning policy, and infrastructure. This file covers only this service's internals.

---

## Build & Deploy

```bash
# Build and push (update version in k8s/agents/05-backend-deployment.yaml first)
docker build -t registry.richardx.dev/amael-agentic-backend:<version> .
docker push registry.richardx.dev/amael-agentic-backend:<version>
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia
```

---

## Development

```bash
# Install all deps including dev/test extras
pip install -e ".[all]"

# Run tests
pytest tests/
pytest tests/unit/agents/test_planner.py -v      # single file
pytest --cov=. --cov-report=html tests/           # with coverage
pytest -m "not e2e" tests/                        # skip e2e

# Lint & format
ruff check . --fix

# Type check (optional)
mypy . --ignore-missing-imports
```

**Ruff config**: `line-length = 100`, Python 3.11 target, select E/F/I/UP rules.
**Pytest**: `asyncio_mode = "auto"`, test files match `test_*.py`.

---

## Architecture

### Request Flow

```
POST /api/chat
  → JWT auth → rate limit (Redis, 15/60s) → validate_prompt()
  → AgentRouter.route()          # keyword match (0.9 conf) or LLM fallback
  → _build_tools_map(user_id)    # per-request skill callables
  → AgentDispatcher.dispatch()
       ├─ Direct path (intent=sre/productivity/research)
       │   → AgentRegistry.get(name) → Agent.run()
       └─ Pipeline path (general/k8s/monitoring/etc)
           → run_workflow() → LangGraph
  → sanitize_output() → ChatResponse
```

### LangGraph Pipeline

```
planner → grouper → batch_executor (loop) → supervisor
                                               ↓ REPLAN (max 1 retry)
                                               ↓ ACCEPT → END
```

- **PlannerAgent** (`agents/planner/agent.py`): LLM generates JSON plan, capped at `MAX_PLAN_STEPS=8`
- **Grouper** (`agents/planner/grouper.py`): Groups consecutive non-REASONING steps into parallel batches
- **ExecutorAgent** (`agents/executor/agent.py`): Runs batches — tool steps in parallel (`ThreadPoolExecutor`), REASONING sequentially
- **SupervisorAgent** (`agents/supervisor/agent.py`): Scores 0–10, REPLAN if < 6

**Graph caching**: Compiled once in `orchestration/workflow_engine.py` at startup. `tools_map` injected per-request via `AgentState` — this is the pattern that keeps the graph cacheable despite per-user skills.

### Step Types

| Type | Executor behavior |
|------|------------------|
| `K8S_TOOL` | HTTP call to k8s-agent-service:8002 |
| `RAG_RETRIEVAL` | Qdrant semantic search, optional filename filter |
| `PRODUCTIVITY_TOOL` | Google Calendar/Gmail via Vault OAuth |
| `WEB_SEARCH` | DuckDuckGo |
| `REASONING` | LLM reflection, always sequential, with language detection |

### Direct Agents

- **SREAgent** (`agents/sre/agent.py`): APScheduler 60s autonomous loop (Observe → Detect → Diagnose → Decide → Act). Also reachable via direct intent routing.
- **ProductivityAgent** (`agents/productivity/agent.py`): Calendar/Gmail; reads OAuth tokens from Vault (`secret/data/amael/google-tokens/*`).
- **ResearchAgent** (`agents/researcher/agent.py`): RAG + DuckDuckGo. `rag_retriever.py` uses `client.scroll()` + Python substring for filename filtering (not Qdrant `MatchText` — no FTS index).

### Registries

All three registries use a singleton + decorator pattern and populate during lifespan startup:

| Registry | Module | Startup fn |
|----------|--------|-----------|
| `AgentRegistry` | `agents/base/agent_registry.py` | `register_all_agents()` |
| `SkillRegistry` | `skills/registry.py` | `register_all_skills()` |
| `ToolRegistry` | `tools/registry.py` | `register_all_tools()` |

### Core Abstractions

- `core/agent_base.py` — `BaseAgent` with `before/execute/after/on_error` lifecycle, `AgentContext`, `AgentResult`
- `core/skill_base.py` — `BaseSkill` (stateless, Pydantic I/O)
- `core/tool_base.py` — `BaseTool` (external integrations)
- `core/constants.py` — `StepType`, `ActionType`, `AnomalyType`, `Severity` enums; `MAX_PLAN_STEPS=8`, `MAX_GRAPH_ITERATIONS=10`

---

## Storage

| Store | Purpose | Client module |
|-------|---------|--------------|
| PostgreSQL | conversations, messages, sre_incidents, sre_learning_stats | `storage/postgres/client.py` |
| Redis | rate limiting, SRE dedup, maintenance windows, session | `storage/redis/client.py` |
| Qdrant | per-user RAG collections + `sre_runbooks` collection | direct `qdrant_client` |
| MinIO | uploaded document backup (`amael-uploads` bucket) | direct `minio` |

**Qdrant**: Use `query_points()` (v1.7+ API). The codebase has an `AttributeError` fallback to `search()` for older versions.

**PostgreSQL schema** is auto-applied at startup via `_ensure_schema()` in `main.py`.

---

## Security

- `security/validator.py`: 4000 char limit, injection pattern blocking, control char strip
- `security/sanitizer.py`: redacts `hvs.*` Vault tokens, JWTs, `password=`/`secret=` assignments
- Internal endpoints use `require_internal_secret()` (Bearer header, exact match)
- Bot user `bot-amael@richardx.dev` can pass `user_id` in body to dispatch as real user

---

## Observability

- **Metrics**: `observability/metrics.py` → Prometheus on `:8000/metrics`
- **Tracing**: `observability/tracing.py` → OTel gRPC → `otel-collector.observability:4317`
- **Logging**: `observability/logging.py` → JSON structured with `request_id`, `user_id`, `conversation_id` context vars
- **Health**: `GET /health` checks Postgres, Redis, Qdrant, Ollama dependencies

---

## RAG Retrieval Details

`agents/researcher/rag_retriever.py`:
1. `_detect_filename_filter()` — compares significant query words (>3 chars, non-stopword) against indexed filenames
2. If filter: `client.scroll(limit=500)` + Python substring match on `metadata.filename`/`source`
3. Rerank via numpy cosine similarity
4. If no filter: `vectorstore.similarity_search(k=5)`

Post-REASONING language detection (v1.10.13): translation fires when target language is ES (explicit preference OR detected from question) AND response is NOT Spanish (covers "und"/mixed — not just "en"). `PATCH /api/memory/profile/language` sets preference, invalidates Redis cache immediately.

---

## Key Env Vars

```
# LLM
OLLAMA_BASE_URL=http://ollama-service:11434
LLM_MODEL=qwen3:14b
LLM_EMBED_MODEL=nomic-embed-text

# Security
INTERNAL_API_SECRET=...   # Bearer token for CronJob/WhatsApp/SRE internal calls
JWT_SECRET_KEY=...

# Services
K8S_AGENT_URL=http://k8s-agent-service:8002
PRODUCTIVITY_SERVICE_URL=http://productivity-service:8001

# Databases
POSTGRES_HOST/DB/USER/PASSWORD
REDIS_HOST, QDRANT_URL, MINIO_ENDPOINT
```

Full list in `config/settings.py`.

---

## Known Gotchas

- **`from __future__ import annotations`** is required in files that use forward-referenced type annotations (e.g., `-> List[Anomaly]` before the `Anomaly` dataclass is defined). Missing this causes `NameError` on Python 3.9.
- **nomic-embed-text** must be pulled in Ollama. Missing model silently returns `[]` from embedding calls.
- **Qdrant `search()` removed in v1.7+**: always use `query_points()`.
- **WhatsApp bridge** uses `strategy: Recreate` (not RollingUpdate) to avoid Chromium `SingletonLock` on the session PVC.
- **Ollama restarts**: Use `kubectl delete pod -l app=ollama -n amael-ia` (not `rollout restart`) — RollingUpdate leaves new pod Pending with a single GPU.
