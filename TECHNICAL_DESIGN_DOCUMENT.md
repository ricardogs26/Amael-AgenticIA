# Technical Design Document — Amael-AgenticIA

**Versión:** 1.4.5
**Fecha:** 2026-03-15
**Autor:** Ricardo Guzmán
**Estado:** Producción

---

## Tabla de Contenidos

1. [Resumen Ejecutivo](#1-resumen-ejecutivo)
2. [Contexto y Motivación](#2-contexto-y-motivación)
3. [Arquitectura General](#3-arquitectura-general)
4. [Stack Tecnológico](#4-stack-tecnológico)
5. [Pipeline LangGraph](#5-pipeline-langgraph)
6. [Agentes Especializados](#6-agentes-especializados)
7. [Sistema RAG (Retrieval-Augmented Generation)](#7-sistema-rag)
8. [API REST](#8-api-rest)
9. [Capa de Seguridad](#9-capa-de-seguridad)
10. [Almacenamiento](#10-almacenamiento)
11. [Observabilidad](#11-observabilidad)
12. [Infraestructura Kubernetes](#12-infraestructura-kubernetes)
13. [Servicios de Soporte](#13-servicios-de-soporte)
14. [Flujos de Datos End-to-End](#14-flujos-de-datos-end-to-end)
15. [Decisiones de Diseño y Trade-offs](#15-decisiones-de-diseño-y-trade-offs)
16. [Limitaciones Conocidas](#16-limitaciones-conocidas)
17. [Roadmap](#17-roadmap)

---

## 1. Resumen Ejecutivo

**Amael-AgenticIA** es una plataforma de inteligencia artificial multi-agente diseñada para automatización inteligente en entornos empresariales. Combina un motor de orquestación basado en **LangGraph** con agentes especializados que pueden consultar infraestructura Kubernetes, buscar en bases de conocimiento personales, gestionar productividad (calendario/email), explorar la web y operar de forma autónoma como SRE.

### Características Principales

| Capacidad | Descripción |
|-----------|-------------|
| **Orquestación multi-agente** | Pipeline Planner → Grouper → Executor → Supervisor con re-plan automático |
| **RAG per-usuario** | Búsqueda semántica sobre documentos privados indexados en Qdrant |
| **K8s nativo** | Consulta real de pods, deployments, logs y métricas del clúster |
| **Productividad** | Integración Google Calendar / Gmail via OAuth + Vault |
| **SRE autónomo** | Loop de 60s: observa → detecta → diagnostica → actúa → verifica |
| **Streaming** | SSE word-by-word para el frontend Next.js |
| **WhatsApp** | Canal bidireccional via Puppeteer bridge |
| **Observabilidad** | Prometheus + Grafana (8 dashboards) + OpenTelemetry + Tempo |

### Métricas de Producción

- **Modelo LLM:** qwen2.5:14b vía Ollama (GPU RTX 5070)
- **Embedding:** nomic-embed-text (768 dims)
- **Rate limit:** 15 req/60s por usuario
- **Max pasos por plan:** 8
- **Max iteraciones del grafo:** 10
- **Timeout LLM diagnosis SRE:** 30s

---

## 2. Contexto y Motivación

### Problema que resuelve

El proyecto evolucionó desde un backend monolítico (`backend-ia`) hacia una arquitectura modular. El `backend-ia` concentraba toda la lógica en un solo archivo `main.py`, dificultando el mantenimiento, las pruebas y la extensión.

**Amael-AgenticIA** resuelve esto con:

- **Separación de responsabilidades**: cada agente, skill y tool vive en su propio módulo
- **Registro explícito**: `AgentRegistry` centraliza el descubrimiento de agentes
- **Estado tipado**: `AgentState` TypedDict fluye a través del grafo sin mutaciones implícitas
- **Configuración centralizada**: `config/settings.py` con Pydantic Settings — un único punto de verdad
- **Contrato base**: `BaseAgent` con ciclo de vida completo (`before → execute → after / on_error`)

### Usuarios del sistema

| Tipo | Acceso | Identificación |
|------|--------|----------------|
| Usuarios humanos | Frontend Next.js + WhatsApp | Email Google OAuth / número WhatsApp |
| Bot de servicio | Interno | `bot-amael@richardx.dev` (JWT fijo) |
| SRE autónomo | Internal k8s-agent API | `INTERNAL_API_SECRET` |
| Admin | Todos los canales | `521XXXXXXXXXX` (número de admin) |

---

## 3. Arquitectura General

### Diagrama de Alto Nivel

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        CLIENTES / INTERFACES                             │
│                                                                          │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────────────────┐  │
│  │ frontend-next│  │ whatsapp-bridge  │  │   CronJob Day Planner     │  │
│  │  (Next.js)   │  │  (Puppeteer)     │  │  (POST /api/planner/daily)│  │
│  └──────┬───────┘  └────────┬─────────┘  └────────────┬──────────────┘  │
│         │ SSE/REST          │ REST                     │ REST            │
└─────────┼───────────────────┼──────────────────────────┼─────────────────┘
          │                   │                          │
          ▼                   ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     AMAEL-AGENTIC-BACKEND (FastAPI :8000)               │
│                                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ /api/chat  │  │ /api/ingest  │  │ /api/conv │  │  /api/planner    │ │
│  │ /api/chat/ │  │              │  │ ersations │  │  /api/profile    │ │
│  │   stream   │  │              │  │           │  │  /api/admin      │ │
│  └─────┬──────┘  └──────┬───────┘  └─────┬─────┘  └────────┬─────────┘ │
│        │                │                 │                  │           │
│        ▼                ▼                 ▼                  ▼           │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │              LANGGRAPH ORCHESTRATION ENGINE                      │   │
│  │                                                                  │   │
│  │  planner → grouper → batch_executor (loop) → supervisor         │   │
│  │      ↑                                            │              │   │
│  │      └──────────── REPLAN (max 1 retry) ──────────┘              │   │
│  └────────────────────────────────────────────────────────────────-─┘   │
│        │                │                 │                  │           │
│        ▼                ▼                 ▼                  ▼           │
│  ┌──────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │K8S_TOOL  │  │RAG_RETRIEVAL│  │PRODUCTIVITY │  │  WEB_SEARCH /   │  │
│  │k8s-agent │  │  Qdrant     │  │  -service   │  │ DOCUMENT_TOOL   │  │
│  │  :8002   │  │  :6333      │  │    :8001    │  │  DuckDuckGo     │  │
│  └──────────┘  └─────────────┘  └─────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          STORAGE LAYER                                   │
│                                                                          │
│  PostgreSQL  │  Redis        │  Qdrant          │  MinIO                 │
│  (historia,  │  (sesiones,   │  (RAG per-user,  │  (backup docs)        │
│   usuarios,  │  rate-limit,  │  SRE runbooks)   │                       │
│   incidentes)│  SRE dedup)   │                  │                       │
└─────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         LLM INFERENCE                                    │
│                                                                          │
│            Ollama :11434 — qwen2.5:14b + nomic-embed-text               │
│                      GPU: NVIDIA RTX 5070 (exclusiva)                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Estructura de Directorios

```
Amael-AgenticIA/
├── main.py                          # Entry point FastAPI + lifespan startup
├── config/
│   └── settings.py                  # Pydantic Settings — todas las env vars
├── core/
│   ├── agent_base.py                # BaseAgent, AgentContext, AgentResult
│   ├── skill_base.py                # BaseSkill (contrato de skills)
│   ├── tool_base.py                 # BaseTool (contrato de tools)
│   ├── constants.py                 # MAX_PLAN_STEPS=8, MAX_GRAPH_ITERATIONS=10
│   ├── exceptions.py                # AgentNotFoundError, AgentDependencyError
│   └── message_types.py             # Tipos de mensaje internos
├── agents/
│   ├── base/agent_registry.py       # AgentRegistry + register_all_agents()
│   ├── planner/
│   │   ├── agent.py                 # PlannerAgent + planner_node() + grouper_node()
│   │   ├── prompts.py               # PLANNER_SYSTEM_PROMPT
│   │   ├── models.py                # PlanStep (Pydantic)
│   │   └── grouper.py               # group_plan_into_batches()
│   ├── executor/
│   │   ├── agent.py                 # ExecutorAgent + batch_executor_node()
│   │   ├── batch_runner.py          # run_reasoning_step(), run_parallel_batch()
│   │   └── step_handlers.py         # STEP_HANDLERS dict
│   ├── supervisor/
│   │   ├── agent.py                 # SupervisorAgent + supervisor_node()
│   │   ├── quality_scorer.py        # evaluate() — lógica de scoring 0-10
│   │   └── prompts.py               # SUPERVISOR_SYSTEM_PROMPT
│   ├── researcher/
│   │   ├── agent.py                 # ResearchAgent
│   │   ├── rag_retriever.py         # retrieve_documents(), ingest_document()
│   │   └── web_searcher.py          # Búsqueda DuckDuckGo
│   ├── productivity/
│   │   ├── agent.py                 # ProductivityAgent
│   │   ├── calendar_manager.py      # Google Calendar API
│   │   ├── email_manager.py         # Gmail API
│   │   ├── day_planner.py           # Resumen diario + agenda
│   │   └── vault_credentials.py     # Obtención de tokens desde Vault
│   └── sre/
│       ├── agent.py                 # SREAgent + loop APScheduler
│       ├── observer.py              # observe_cluster/metrics/trends/slo
│       ├── diagnoser.py             # diagnose_with_llm()
│       ├── healer.py                # execute_sre_action()
│       ├── reporter.py              # store_incident(), postmortem LLM
│       ├── scheduler.py             # APScheduler setup
│       └── models.py                # Anomaly, Incident dataclasses
├── orchestration/
│   ├── state.py                     # AgentState TypedDict + initial_state()
│   ├── workflow_engine.py           # get_workflow(), run_workflow()
│   ├── agent_router.py              # AgentRouter — routing intent detection
│   ├── agent_dispatcher.py          # dispatch() — ejecuta workflow o fast-path
│   └── context_factory.py           # Construye AgentContext por request
├── interfaces/api/
│   ├── auth.py                      # get_current_user(), check_rate_limit()
│   └── routers/
│       ├── chat.py                  # POST /api/chat, POST /api/chat/stream
│       ├── ingest.py                # POST /api/ingest
│       ├── conversations.py         # GET/DELETE /api/conversations
│       ├── identity.py              # GET /api/identity/check
│       ├── planner.py               # POST /api/planner/daily
│       ├── sre.py                   # GET /api/sre/* (incidents, postmortems)
│       ├── feedback.py              # POST /api/feedback
│       ├── auth.py                  # GET /auth/login, /auth/callback
│       ├── profile.py               # GET/PUT /api/profile
│       └── admin.py                 # GET /api/admin/*
├── skills/
│   ├── registry.py                  # SkillRegistry + register_all_skills()
│   ├── kubernetes/skill.py          # KubernetesSkill — lectura estado K8s
│   ├── rag/skill.py                 # RAGSkill — Qdrant search/ingest
│   ├── llm/skill.py                 # LLMSkill — ChatOllama wrapper
│   ├── vault/skill.py               # VaultSkill — lectura secretos
│   └── web/skill.py                 # WebSkill — búsqueda DuckDuckGo
├── tools/
│   ├── registry.py                  # ToolRegistry + register_all_tools()
│   ├── prometheus/                  # PrometheusTool — PromQL queries
│   ├── grafana/                     # GrafanaTool — screenshots dashboards
│   ├── whatsapp/                    # WhatsAppTool — POST /send
│   └── github/                      # GitHubTool — repos, PRs, issues
├── storage/
│   ├── postgres/client.py           # psycopg2 pool, get_connection()
│   ├── redis/client.py              # redis.Redis singleton
│   └── minio/client.py              # minio.Minio singleton
├── security/
│   ├── validator.py                 # validate_prompt() — max 4000 chars + patterns
│   └── sanitizer.py                 # sanitize_output() — redacta tokens/secrets
├── observability/
│   ├── logging.py                   # setup_logging() — JSON en prod
│   ├── metrics.py                   # Todas las métricas Prometheus
│   ├── tracing.py                   # OpenTelemetry → otel-collector
│   ├── middleware.py                # ObservabilityMiddleware (latencia HTTP)
│   └── health.py                    # build_health_router() — /health/*
└── k8s/agents/
    └── 05-backend-deployment.yaml   # Deployment + Service Kubernetes
```

---

## 4. Stack Tecnológico

### Backend Core

| Componente | Tecnología | Versión | Propósito |
|------------|-----------|---------|-----------|
| Framework web | FastAPI | 0.110+ | API REST + SSE streaming |
| Orquestación agentes | LangGraph | 0.1+ | StateGraph compilado y cacheado |
| LLM (chat) | ChatOllama | langchain-ollama | Planner, Supervisor, Reasoning |
| LLM (completion) | OllamaLLM | langchain-ollama | Resúmenes, documentos |
| Embeddings | OllamaEmbeddings | langchain-ollama | nomic-embed-text 768 dims |
| Validación config | Pydantic Settings | v2 | Variables de entorno tipadas |
| Vector DB client | qdrant-client | 1.7+ | RAG per-usuario |
| PDF parsing | PyPDFLoader | langchain-community | Ingesta de documentos |
| DOCX parsing | python-docx | - | Ingesta documentos Word |
| MIME detection | python-magic | - | Detección tipo de archivo |
| Chunking | RecursiveCharacterTextSplitter | langchain-text-splitters | 1000 chars, overlap 200 |
| Auth OAuth | Authlib | - | Google OAuth2 flow |
| JWT | python-jose | - | Tokens de sesión |
| HTTP client | httpx | - | Llamadas a servicios internos |
| Servidor ASGI | uvicorn | - | Producción + desarrollo |

### Modelos LLM (Ollama)

| Modelo | Uso | Parámetros |
|--------|-----|------------|
| `qwen2.5:14b` | Chat: planner, supervisor, reasoning, SRE diagnosis | 14B |
| `qwen2.5-vl:7b` | Visión (Grafana screenshots) | 7B |
| `nomic-embed-text` | Embeddings RAG (768 dims) | - |

---

## 5. Pipeline LangGraph

### Grafo de Ejecución

```
                    ┌─────────┐
             ┌─────►│ planner │
             │      └────┬────┘
             │           │ genera plan JSON ["STEP: descripción", ...]
             │           ▼
             │      ┌─────────┐
             │      │ grouper │  — agrupa pasos consecutivos no-REASONING
             │      └────┬────┘     en batches paralelos
             │           │
             │           ▼
             │   ┌───────────────┐
             │   │batch_executor │◄─── loop hasta current_batch >= total
             │   └───────┬───────┘
             │           │
             │    MAX_GRAPH_ITERATIONS?
             │    (10) ──┐
             │           │ no → batch_executor (loop)
             │           │ sí → supervisor (forzado)
             │           ▼
             │      ┌──────────┐
             │      │supervisor│  — evalúa score 0-10
             │      └────┬─────┘
             │           │
             │      score < 6?
             │      retry < MAX?
             └──────────YES
                         │
                        NO → END
```

### AgentState

El estado es un `TypedDict` que fluye sin mutaciones a través de todos los nodos:

```python
class AgentState(TypedDict):
    question:           str         # Pregunta original del usuario
    plan:               List[str]   # ["K8S_TOOL: ...", "REASONING: ..."]
    batches:            List[List[str]]  # Plan agrupado en batches
    current_batch:      int         # Índice del batch actual
    current_step:       int         # Total pasos ejecutados
    context:            str         # Contexto RAG acumulado (max 12k chars)
    tool_results:       List[Dict]  # Resultados de herramientas
    final_answer:       Optional[str]   # Respuesta en construcción
    user_id:            str
    retry_count:        int         # Re-plans disparados (max 1)
    supervisor_score:   int         # 0-10
    supervisor_reason:  str
    supervisor_decision: str        # "ACCEPT" | "REPLAN"
    tools_map:          Dict        # {name: callable} — inyectado por request
    request_id:         str         # UUID para correlación/tracing
    conversation_id:    str
    routing_intent:     str         # Intent del AgentRouter
    agents_invoked:     List[str]
```

**Diseño clave**: `tools_map` se inyecta en el estado por request, no en el grafo. Esto permite que el grafo compilado sea cacheado una sola vez al startup — sin recompilar por cada request o por cambios en tools.

### Tipos de Pasos del Plan

| Tipo | Handler | Descripción |
|------|---------|-------------|
| `K8S_TOOL` | `handle_k8s_tool()` | Consulta k8s-agent (:8002) — pods, logs, métricas |
| `RAG_RETRIEVAL` | `handle_rag_retrieval()` | Búsqueda semántica en Qdrant del usuario |
| `PRODUCTIVITY_TOOL` | `handle_productivity_tool()` | Google Calendar/Gmail via productivity-service |
| `WEB_SEARCH` | `handle_web_search()` | DuckDuckGo — información actual/externa |
| `DOCUMENT_TOOL` | `handle_document_tool()` | Redacción de documentos formales |
| `REASONING` | `run_reasoning_step()` | Síntesis LLM del contexto acumulado |

### Ejecución Paralela de Batches

El `grouper` agrupa pasos consecutivos **no-REASONING** en batches paralelos:

```
Plan:    [K8S_TOOL:A, K8S_TOOL:B, RAG_RETRIEVAL:C, REASONING:D, K8S_TOOL:E]
Batches: [[A, B, C], [D], [E]]
         ↑ paralelo   ↑ sequential ↑ paralelo
```

Los batches paralelos usan `ThreadPoolExecutor(max_workers=len(batch))`. El REASONING siempre es secuencial (síntesis del contexto acumulado).

### Supervisor y Re-plan

```python
# quality_scorer.py — lógica simplificada
def evaluate(state, redis_client):
    answer = state.get("final_answer", "")

    # Criterios de penalización
    if not answer or len(answer) < 50:              score -= 3
    if "no se encontró información" in answer:      score -= 2
    if "no tengo información" in answer:            score -= 1
    if has_code_block and len(answer) > 200:        score += 2

    decision = "REPLAN" if score < 6 and retry < MAX_RETRIES else "ACCEPT"

    # Guarda feedback en Redis para aprendizaje
    if redis_client:
        redis_client.lpush("supervisor:feedback", json.dumps({...}))
```

---

## 6. Agentes Especializados

### Contrato BaseAgent

Todos los agentes heredan de `BaseAgent` e implementan un ciclo de vida completo:

```python
class BaseAgent(ABC):
    name: str           # ID único en el registry
    role: str           # Descripción del rol
    version: str        # Semver
    capabilities: List[str]       # Capacidades declaradas
    required_skills: List[str]    # Validadas en __init__
    required_tools: List[str]     # Validadas en __init__

    async def run(task) -> AgentResult:
        await self.before_execute(task)   # Hook: logging, OTel span
        result = await self.execute(task)  # Implementación del agente
        await self.after_execute(task, result)  # Hook: métricas, cleanup
        return result                     # AgentResult estandarizado

    # En excepción:
    async def on_error(task, error) -> AgentResult:
        # Log + AgentResult(success=False)
```

### PlannerAgent

**Responsabilidad**: Descomponer el request del usuario en un plan ejecutable.

**Implementación**:
- Usa `ChatOllama` con `SystemMessage`/`HumanMessage` separados (prevención prompt injection)
- El `PLANNER_SYSTEM_PROMPT` define 6 tipos de pasos, 9 reglas estrictas y ejemplos de output válido
- Retorna JSON puro: `["K8S_TOOL: revisar pods", "REASONING: explicar resultado"]`
- Fallback: si el parseo falla → `["REASONING: responder de forma general"]`
- Fast-paths: Grafana/imagen → plan hardcoded de 2 pasos sin llamar al LLM
- Validación con Pydantic `PlanStep.from_string()` — descarta pasos malformados

**Métricas**: `PLANNER_LATENCY_SECONDS`, `PLANNER_PLAN_SIZE`, `PLANNER_PARSE_ERRORS_TOTAL`, `PLANNER_STEP_TYPES_TOTAL`

### ExecutorAgent

**Responsabilidad**: Ejecutar los batches del plan, coordinar herramientas y sintetizar resultados.

**REASONING step** (`run_reasoning_step`):
- Usa `ChatOllama` con `SystemMessage` de idioma + reglas de formato
- Recibe el contexto acumulado (`final_answer` + `context` del estado)
- Post-detección de idioma: si pregunta=ES y respuesta=EN → segundo LLM call de traducción
- Preserva tags `[MEDIA:...]` antes del truncado y los restaura al final

**Parallel batch** (`run_parallel_batch`):
- `ThreadPoolExecutor(max_workers=N)` donde N = tamaño del batch
- Thread-safe: handlers solo leen el estado, nunca lo escriben

**Truncado**: contexto máx 12k chars, respuestas máx 8k chars — con métricas de truncado.

### SupervisorAgent

**Responsabilidad**: Evaluar la calidad de la respuesta final y decidir ACCEPT o REPLAN.

- Scoring heurístico 0-10 basado en longitud, presencia de código, menciones de "no encontré"
- Guarda feedback en Redis (`supervisor:feedback` lista) para análisis futuro
- Máximo 1 re-plan por request (`MAX_RETRIES = 1`)

### ResearchAgent

**Responsabilidad**: RAG sobre documentos del usuario + búsqueda web.

- Accede a Qdrant colección del usuario (`sanitize_email(email)`)
- `_detect_filename_filter()`: detecta si el query referencia un documento específico
- Reranking semántico con numpy cosine similarity cuando hay filtro de documento

### ProductivityAgent

**Responsabilidad**: Google Calendar y Gmail.

- Recupera OAuth tokens desde **HashiCorp Vault** (`secret/data/amael/google-tokens/{email}`)
- `calendar_manager.py`: listar eventos, crear evento, buscar disponibilidad
- `email_manager.py`: leer bandeja, enviar email, responder thread
- `day_planner.py`: resumen diario enviado vía WhatsApp a las 7am

### SREAgent

**Responsabilidad**: Autonomía operacional del clúster Kubernetes.

Loop APScheduler (60s):
```
observe_cluster()     → pods, nodos, restart counts
observe_metrics()     → Prometheus: CPU%, memory%, HTTP 5xx rate
observe_trends()      → predict_linear (disk), deriv (memory leak, error escalation)
observe_slo()         → error budget burn rate por endpoint
detect_anomalies()    → cruza observaciones con umbrales
correlate_anomalies() → agrupa anomalías multi-pod del mismo deployment
diagnose_with_llm()   → LLM + runbooks Qdrant + fallback determinístico
decide_action()       → ROLLOUT_RESTART o NOTIFY_HUMAN (guardrails)
execute_sre_action()  → kubectl rollout restart / WhatsApp notification
store_incident()      → PostgreSQL sre_incidents
_schedule_verification() → verifica 5min después (auto-rollback si falla)
```

---

## 7. Sistema RAG

### Arquitectura de Almacenamiento

```
Usuario sube documento (PDF/TXT/DOCX/MD)
              │
              ▼
   POST /api/ingest
              │
    ┌─────────┴──────────┐
    │ 1. MIME detection  │  python-magic
    │ 2. Text extraction │  PyPDFLoader / python-docx / UTF-8
    │ 3. Chunking        │  1000 chars, overlap 200
    │ 4. metadata embed  │  chunk.metadata["filename"] = file.filename
    │ 5. Qdrant index    │  colección per-user + nomic-embed-text 768d
    │ 6. LLM summary     │  OllamaLLM qwen2.5:14b
    │ 7. PostgreSQL meta │  user_documents tabla
    │ 8. MinIO backup    │  best-effort, no bloquea respuesta
    └────────────────────┘
```

### Colecciones Qdrant

Cada usuario tiene su propia colección:
- **Nombre**: `email.replace("@", "_at_").replace(".", "_dot_")`
- **Tamaño vector**: 768 (nomic-embed-text)
- **Distancia**: Cosine
- **Recreación automática**: si la colección existe con dimensiones incorrectas → delete + recreate

### Flujo de Recuperación

```python
def retrieve_documents(user_email, query, k=5, filename_filter=None):

    # 1. Detectar si el query menciona un documento específico
    effective_filter = filename_filter or _detect_filename_filter(query, user_email)

    if effective_filter:
        # 2a. Scroll 500 puntos SIN filtro Qdrant
        all_points = client.scroll(limit=500, with_vectors=False)

        # 2b. Filtro Python substring en metadata.filename / source
        matched = [p for p in all_points
                   if keyword in p.payload["metadata"]["filename"].lower()]

        # 2c. Reranking semántico con numpy cosine similarity
        query_vec = embeddings.embed_query(query)
        chunk_vecs = embeddings.embed_documents([d.page_content for d in matched])
        docs = sorted_by_cosine_sim[:k]

    else:
        # 2b'. Búsqueda global vectorial
        docs = vectorstore.similarity_search(query, k=k)

    # 3. Formatear con headers [Fuente: filename, pág. N]
    return "\n\n".join(f"[Fuente: {src}]\n{content}" for doc in docs)
```

### Detección de Filename

`_detect_filename_filter()`:
1. Hace scroll de los primeros 200 puntos para obtener filenames indexados
2. Extrae palabras significativas del query (>3 chars, no stopword en ES/EN)
3. Compara con palabras del filename (split por `-_.`)
4. Umbral: 1 palabra en común → retorna la keyword (no el filename completo)
5. La keyword se usa para substring match (más robusto que MatchText de Qdrant)

**Nota**: No se usa `MatchText` de Qdrant porque requiere índice FTS (Full-Text Search) que no está configurado. El approach scroll+Python es menos eficiente pero confiable.

---

## 8. API REST

### Endpoints Principales

| Endpoint | Método | Auth | Rate Limit | Descripción |
|----------|--------|------|------------|-------------|
| `/api/chat` | POST | JWT | 15/60s | Chat bloqueante — retorna JSON |
| `/api/chat/stream` | POST | JWT | 15/60s | Chat SSE streaming — word-by-word |
| `/api/ingest` | POST | JWT | - | Upload documento PDF/TXT/DOCX/MD |
| `/api/conversations` | GET | JWT | - | Lista conversaciones del usuario |
| `/api/conversations/{id}` | GET | JWT | - | Mensajes de una conversación |
| `/api/conversations/{id}` | DELETE | JWT | - | Elimina conversación |
| `/api/planner/daily` | POST | Internal | - | Day Planner — CronJob 7am |
| `/api/identity/check` | GET | JWT | - | Verifica si número/email está autorizado |
| `/api/sre/incidents` | GET | JWT | - | Últimos incidentes SRE |
| `/api/sre/postmortems` | GET | JWT | - | Postmortems LLM generados |
| `/api/profile` | GET/PUT | JWT | - | Perfil de usuario |
| `/api/admin/*` | GET | JWT+Admin | - | Endpoints de administración |
| `/auth/login` | GET | - | - | Redirect Google OAuth |
| `/auth/callback` | GET | - | - | Callback OAuth → JWT |
| `/health` | GET | - | - | Health check básico |
| `/metrics` | GET | - | - | Prometheus metrics |

### Flujo POST /api/chat

```
1. get_current_user()     — validar JWT → user_id
2. check_rate_limit()     — Redis counter por usuario (15/60s)
3. validate_prompt()      — max 4000 chars, strip control chars, block injection
4. AgentRouter.route()    — detecta intent (k8s, sre, rag, productivity, general)
5. ToolRegistry.names()   — obtiene tools disponibles para el tools_map
6. dispatch()             — ejecuta workflow LangGraph o fast-path
7. sanitize_output()      — redacta Vault tokens, JWTs, secrets en la respuesta
8. _persist_message()     — guarda en PostgreSQL (best-effort, no bloquea)
9. → ChatResponse(answer, response, conversation_id, intent, elapsed_ms)
```

### SSE Streaming (/api/chat/stream)

```
data: {"type": "status", "msg": "Analizando tu pregunta…"}
data: {"type": "status", "msg": "Procesando respuesta…"}
data: {"type": "token",  "content": "La "}
data: {"type": "token",  "content": "respuesta "}
data: {"type": "token",  "content": "es..."}
data: {"type": "done"}
```

Tokens emitidos con delay de 12ms entre palabras (efecto typewriter). Header `X-Accel-Buffering: no` para desactivar buffering nginx.

### Compatibilidad WhatsApp Bridge

El bot de servicio (`bot-amael@richardx.dev`) puede pasar el número real del usuario en `body.user_id`. El endpoint detecta esto y usa el usuario real para:
- Rate limiting correcto (por usuario, no por bot)
- RAG en la colección correcta
- Persistencia del historial

---

## 9. Capa de Seguridad

### Autenticación y Autorización

```
┌────────────────────────────────────────────────────────┐
│ 1. Google OAuth2 (Authlib)                             │
│    /auth/login → Google → /auth/callback → JWT         │
│                                                        │
│ 2. JWT Validation (python-jose, HS256)                 │
│    Authorization: Bearer <token>                       │
│    → get_current_user() → user_id (email)              │
│                                                        │
│ 3. Whitelist (ConfigMap 03.5-allowed-users-configmap)  │
│    allowed_emails + allowed_numbers → full_whitelist   │
│                                                        │
│ 4. K8S_TOOL whitelist separada (k8s_allowed_users)     │
│    Solo usuarios autorizados pueden consultar K8s      │
│                                                        │
│ 5. INTERNAL_API_SECRET (Header para servicios internos)│
│    k8s-agent: Authorization: Bearer {INTERNAL_SECRET}  │
└────────────────────────────────────────────────────────┘
```

### Validación de Input (`security/validator.py`)

```python
def validate_prompt(text: str) -> Tuple[bool, str]:
    # 1. Longitud máxima: 4000 caracteres
    # 2. Strip de caracteres de control
    # 3. Patrones de injection bloqueados:
    #    - "ignore previous instructions"
    #    - "system prompt"
    #    - "you are now" + "act as"
    #    - comandos inyectados: "\n\nHuman:", "\n\nAssistant:"
```

### Sanitización de Output (`security/sanitizer.py`)

Redacta automáticamente en la respuesta final:
- Tokens Vault: `hvs.[a-zA-Z0-9]+` → `hvs.***REDACTED***`
- JWTs: patrón `xxxxx.yyyyy.zzzzz` → `***JWT_REDACTED***`
- Asignaciones: `password=valor`, `secret=valor` → `password=***`

### Rate Limiting

- Redis counter: `rate_limit:{user_id}` con TTL de 60s
- 15 requests/60s por usuario
- HTTP 429 Too Many Requests si se excede
- Aplica tanto a `/api/chat` como `/api/chat/stream`

### Separación System/Human Prompt

El PlannerAgent usa `SystemMessage(PLANNER_SYSTEM_PROMPT)` + `HumanMessage(question)` — el input del usuario nunca toca el system prompt. Esto es fundamental para prevenir prompt injection en la fase de planificación.

---

## 10. Almacenamiento

### PostgreSQL

**Driver**: psycopg2 con connection pool (min=2, max=10)

**Tablas principales**:

```sql
-- Historial de conversaciones
CREATE TABLE conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_conversations_user ON conversations (user_id, created_at DESC);

-- Mensajes individuales
CREATE TABLE messages (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,   -- 'user' | 'assistant'
    content          TEXT,
    intent           TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at ASC);

-- Documentos ingresados
CREATE TABLE user_documents (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    doc_type     TEXT,          -- filename
    summary      TEXT,          -- resumen LLM
    raw_analysis TEXT,          -- primeros 10k chars del documento
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Incidentes SRE
CREATE TABLE sre_incidents (
    id          SERIAL PRIMARY KEY,
    issue_type  TEXT,
    severity    TEXT,
    namespace   TEXT,
    resource    TEXT,
    action      TEXT,
    confidence  FLOAT,
    diagnosis   TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

### Redis

**Driver**: redis-py, singleton

**Keys utilizados**:

| Key Pattern | Tipo | TTL | Propósito |
|-------------|------|-----|-----------|
| `rate_limit:{user_id}` | Counter (INCR) | 60s | Rate limiting por usuario |
| `supervisor:feedback` | List (LPUSH) | - | Feedback para análisis |
| `sre:dedup:{hash}` | String | 5min | Dedup de anomalías SRE |
| `sre:maintenance` | String | configurable | Ventana de mantenimiento |
| `sre:cb:failures` | Counter | 5min | Circuit breaker SRE |
| `sre:cb:open` | String | 5min | Estado circuit breaker |

### Qdrant

**Driver**: qdrant-client + langchain-qdrant

**Colecciones**:

| Colección | Propósito | Dims | Distancia |
|-----------|-----------|------|-----------|
| `{email_sanitized}` | RAG per-usuario | 768 | Cosine |
| `sre_runbooks` | Runbooks SRE (7 archivos MD) | 768 | Cosine |

**Payload structure por punto**:
```json
{
  "page_content": "texto del chunk",
  "metadata": {
    "filename": "DevOps_Guide.pdf",
    "source": "/tmp/uuid-DevOps_Guide.pdf",
    "page": 3
  }
}
```

### MinIO

**Driver**: minio-py

**Buckets**: uno por usuario — email sanitizado como nombre del bucket (`:` y `_` → `-`, máx 63 chars).

**Uso**: backup de documentos subidos (best-effort — fallo no bloquea respuesta al usuario).

---

## 11. Observabilidad

### Métricas Prometheus (amael-agentic-backend)

```promql
# Planner
amael_planner_latency_seconds_bucket     # Latencia generación del plan
amael_planner_plan_size                  # Número de pasos en el plan
amael_planner_step_types_total           # Pasos por tipo
amael_planner_parse_errors_total         # Errores de parseo JSON

# Executor
amael_executor_step_latency_seconds      # Latencia por tipo de paso
amael_executor_steps_total               # Total pasos ejecutados
amael_executor_parallel_batch_size       # Tamaño de batches paralelos
amael_executor_parallel_batches_total    # Total batches paralelos
amael_executor_errors_total              # Errores por tipo de paso
amael_executor_estimated_prompt_tokens   # Tokens estimados (len/4)
amael_executor_context_truncations_total # Truncados de contexto

# RAG
amael_rag_hits_total                     # Búsquedas con resultados
amael_rag_miss_total                     # Búsquedas sin resultados

# Supervisor
amael_supervisor_quality_score_bucket    # Scores 0-10
amael_supervisor_decisions_total         # ACCEPT vs REPLAN
amael_orchestrator_max_steps_hit_total   # Veces que se alcanzó MAX_GRAPH_ITERATIONS

# Security
amael_security_rate_limited_total        # Requests bloqueados por rate limit
amael_security_validation_errors_total   # Prompts inválidos rechazados

# HTTP (ObservabilityMiddleware)
http_requests_total{method, endpoint, status}
http_request_duration_seconds{method, endpoint}
```

### Grafana Dashboards

8 dashboards auto-cargados vía sidecar desde ConfigMap `amael-custom-dashboards`:

| # | UID | Contenido |
|---|-----|-----------|
| 1 | `amael-llm` | Latencia LLM, tokens, errores |
| 2 | `amael-agent` | Pipeline: planner, executor, supervisor |
| 3 | `amael-rag` | Hits/miss RAG, latencia de chunks |
| 4 | `amael-infra` | CPU/memory pods, GPU utilization (DCGM) |
| 5 | `amael-supervisor` | Score distribución, replans |
| 6 | `amael-security` | Rate limits, validación errors |
| 7 | `amael-service-map` | Service graph OTel (Tempo) |
| 8 | `amael-sre-agent` | Loop SRE, anomalías, acciones, SLO |

### OpenTelemetry

- **Exportador**: gRPC → `otel-collector.observability:4317`
- **Spans**: `agent.planner`, `agent.executor.batch`, `agent.executor.reasoning`, `agent.executor.{step_type}`
- **Atributos**: `agent.question_length`, `agent.plan_steps`, `agent.step_latency_seconds`
- **Service graph**: Tempo `metricsGenerator` genera métricas `traces_service_graph_*` → Prometheus

### Logging

- **Producción**: JSON estructurado (setup_logging con `jsonformatter`)
- **Desarrollo**: formato legible con colores
- **Contexto por request**: `request_id`, `user_id`, `conversation_id` inyectados via `set_log_context()`

---

## 12. Infraestructura Kubernetes

### Cluster

- **Distribución**: MicroK8s (single-node)
- **Nodo**: `lab-home`
- **Namespace principal**: `amael-ia`
- **GPU**: NVIDIA RTX 5070 (asignada exclusivamente a `ollama-deployment`)

### Deployment: amael-agentic-backend

```yaml
# k8s/agents/05-backend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: amael-agentic-deployment
  namespace: amael-ia
spec:
  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    spec:
      initContainers:
      - name: wait-redis   # espera Redis antes de arrancar
      containers:
      - name: amael-agentic-backend
        image: registry.richardx.dev/amael-agentic-backend:1.4.5
        imagePullPolicy: Always
        ports:
        - name: http
          containerPort: 8000
        envFrom:
        - configMapRef:  # OLLAMA_BASE_URL, MODEL_NAME, QDRANT_URL...
        - secretRef:     # JWT_SECRET_KEY, INTERNAL_API_SECRET, DB passwords
```

### Ingress (Kong Gateway)

```
amael-ia.richardx.dev
├── /api  → amael-agentic-backend:8000   (nuevo backend)
├── /llm  → llm-adapter:80
├── /tts  → cosyvoice-service:8000
└── /     → frontend-next:3000
```

### Secrets (amael-secrets)

| Key | Contenido |
|-----|-----------|
| `jwt-secret-key` | Clave para firmar/verificar JWTs |
| `jwt-token` | Token pre-generado para bot-amael@richardx.dev |
| `internal-api-secret` | Secret compartido entre servicios internos |
| `postgres-password` | Contraseña PostgreSQL |

### ConfigMaps relevantes

| ConfigMap | Namespace | Contenido |
|-----------|-----------|-----------|
| `amael-backend-config` | `amael-ia` | OLLAMA_BASE_URL, MODEL_NAME, QDRANT_URL, REDIS_HOST |
| `03.5-allowed-users-configmap` | `amael-ia` | ALLOWED_EMAILS_CSV, K8S_ALLOWED_USERS_CSV |
| `amael-custom-dashboards` | `observability` | 8 dashboards Grafana JSON |

### Versiones de Imágenes Actuales

| Servicio | Imagen | Versión |
|----------|--------|---------|
| amael-agentic-backend | `registry.richardx.dev/amael-agentic-backend` | `1.4.5` |
| k8s-agent | `registry.richardx.dev/k8s-agent` | `5.0.2` |
| productivity-service | `registry.richardx.dev/productivity-service` | `1.2.0` |
| frontend-next | `registry.richardx.dev/frontend-next` | `1.0.4` |
| whatsapp-bridge | `registry.richardx.dev/whatsapp-bridge` | `1.5.0` |

---

## 13. Servicios de Soporte

### k8s-agent (:8002)

Agente FastAPI especializado en operaciones Kubernetes y SRE autónomo.

**Arquitectura interna**:
- LangGraph `create_react_agent` (primario) + LangChain clásico (fallback)
- APScheduler 60s — loop SRE autónomo
- Kubernetes Lease para leader election (namespace `amael-ia`)
- Circuit breaker (5 fallos → open 5min)

**RBAC**:
- `ClusterRole sre-agent-observer`: read-only en pods, nodos, deployments (todos los namespaces)
- `Role sre-agent-healer`: delete pods, exec, patch deployments, Leases (solo `amael-ia`)

**Endpoints clave**:

| Endpoint | Método | Propósito |
|----------|--------|-----------|
| `/api/k8s-agent` | POST | Agente conversacional (requiere Bearer) |
| `/api/sre/loop/status` | GET | Estado del loop SRE |
| `/api/sre/incidents` | GET | Últimos incidentes |
| `/api/sre/slo/status` | GET | Error budget por endpoint |
| `/api/sre/maintenance` | GET/POST/DELETE | Ventana de mantenimiento |
| `/api/sre/command` | POST | Comandos WhatsApp `/sre *` |

### productivity-service (:8001)

Integración con Google Calendar y Gmail via OAuth2 + Vault.

- Tokens OAuth almacenados en Vault: `secret/data/amael/google-tokens/{email}`
- Service Account `productivity-service-sa` con auth Kubernetes → Vault role `amael-productivity`
- Day Planner: agrega eventos del día + resumen del clima → envía vía WhatsApp

### whatsapp-bridge (:3000)

Bridge Express.js con Puppeteer/Chromium para WhatsApp Web.

**Características**:
- `strategy: Recreate` — Chromium no puede compartir perfil entre pods
- PVC `whatsapp-session-pvc` para persistir sesión entre reinicios
- `/dev/shm` EmptyDir 256Mi para Chromium
- JWT bot: `bot-amael@richardx.dev` con token pre-generado en `amael-secrets`

**Comandos soportados**:

| Comando | Destino | Respuesta |
|---------|---------|-----------|
| Mensaje libre | `POST /api/chat` (backend) | Respuesta del agente |
| `/estado` | k8s-agent | Estado del clúster |
| `/plan` | productivity-service | Agenda del día |
| `/sre status` | k8s-agent `/api/sre/command` | Estado SRE loop |
| `/sre incidents` | k8s-agent | Últimos 5 incidentes |
| `/sre maintenance on 60` | k8s-agent | Activa ventana mantenimiento 60min |

### HashiCorp Vault

- **Namespace**: `vault`, StatefulSet `vault-0`
- **Auth**: Kubernetes ServiceAccount JWT → Vault TokenReview API
- **Rol**: `amael-productivity` → política CRUD en `secret/data/amael/google-tokens/*`
- **Unseal**: Shamir 3-of-5

### Ollama

- **Modelos**: `qwen2.5:14b`, `qwen2.5-vl:7b`, `nomic-embed-text`
- **GPU**: RTX 5070 (única GPU disponible) — `nvidia.com/gpu: 1` en el Deployment
- **Restart**: usar `kubectl delete pod -l app=ollama -n amael-ia` (no `rollout restart` — con RollingUpdate el pod nuevo quedaría Pending esperando la GPU)

---

## 14. Flujos de Datos End-to-End

### Flujo 1: Chat con RAG desde WhatsApp

```
Usuario WhatsApp: "¿Qué dice el documento de DevOps sobre cultura de equipos?"
    │
    ▼
whatsapp-bridge (Express.js)
    │ POST /api/chat body={prompt: "...", user_id: "521XXXXXXXXXX"}
    │ Authorization: Bearer {jwt-token bot}
    ▼
POST /api/chat (FastAPI)
    │ get_current_user() → "bot-amael@richardx.dev"
    │ effective_user = "521XXXXXXXXXX" (del body.user_id)
    │ check_rate_limit("521XXXXXXXXXX")
    │ validate_prompt() → ok
    ▼
AgentRouter.route()
    │ → intent: "rag_document"
    ▼
dispatch() → run_workflow()
    ▼
planner_node()
    │ ChatOllama(SystemMessage=PLANNER_PROMPT, HumanMessage="¿Qué dice...")
    │ → ["RAG_RETRIEVAL: cultura de equipos DevOps", "REASONING: explicar cultura..."]
    ▼
grouper_node()
    │ → batches: [["RAG_RETRIEVAL: cultura de equipos DevOps"], ["REASONING: ..."]]
    ▼
batch_executor_node() — Batch 0: RAG_RETRIEVAL
    │ handle_rag_retrieval("cultura de equipos DevOps", state, tools_map)
    │   _detect_filename_filter() → keyword="devops"
    │   client.scroll(limit=500) → filtra chunks con "devops" en filename
    │   cosine_similarity reranking → top 5 chunks
    │ → "[Fuente: DevOps_Guide.pdf, pág. 3]\n Team culture in DevOps..."
    │   state.final_answer = resultado RAG
    ▼
batch_executor_node() — Batch 1: REASONING
    │ run_reasoning_step("explicar cultura...", state)
    │   ChatOllama([SystemMessage(idioma), HumanMessage(pregunta + contexto)])
    │   → "La cultura de equipos en DevOps se basa en..."
    │   _detect_language(respuesta) == "en" && _detect_language(pregunta) == "es"
    │   → LLM traducción → "La cultura de equipos en DevOps se basa en..."
    ▼
supervisor_node()
    │ evaluate() → score=7 → ACCEPT
    ▼
sanitize_output() → sin tokens/secrets
    ▼
_persist_message() → PostgreSQL conversations + messages
    ▼
ChatResponse(answer="La cultura de equipos...")
    ▼
whatsapp-bridge → POST /send → Usuario WhatsApp recibe respuesta
```

### Flujo 2: Ingesta de Documento PDF

```
Usuario sube PDF desde frontend-next
    │
    ▼
POST /api/ingest
    │ Authorization: Bearer {jwt usuario}
    │ file: UploadFile (PDF)
    ▼
1. MIME detection: python-magic → "application/pdf"
2. Guardar en /tmp/{uuid}-filename.pdf
3. PyPDFLoader → List[LCDocument] con metadata.page
4. RecursiveCharacterTextSplitter(1000, 200) → N chunks
5. chunk.metadata["filename"] = "filename.pdf"  (limpio, sin UUID)
6. get_user_vectorstore(user_email)
   → QdrantVectorStore colección "email_at_domain_dot_com"
7. vectorstore.add_documents(chunks)  ← indexado en Qdrant
8. OllamaLLM.invoke("Resume en 2-3 oraciones: ...") → summary
9. PostgreSQL INSERT user_documents (user_id, filename, summary, raw_text[:10k])
10. MinIO put_object(bucket=email-sanitized, key=filename, data=content)
11. os.remove(temp_path)
    ▼
{ doc_id, filename, summary, chunks: N }
```

### Flujo 3: SRE Autónomo detecta OOM_KILLED

```
APScheduler (cada 60s)
    ▼
sre_autonomous_loop()
    │ Lease check → es líder
    │ Circuit breaker → cerrado
    │ Maintenance window → no activa
    ▼
observe_cluster() → K8s API
    │ pod "backend-ia-xxx" OOMKilled, restarts=5
    ▼
detect_anomalies()
    │ → Anomaly(type=OOM_KILLED, severity=HIGH, resource="backend-ia-xxx")
    ▼
diagnose_with_llm()
    │ Busca runbooks en Qdrant collection "sre_runbooks"
    │ → runbook: oom_killed.md
    │ ChatOllama → diagnosis + confidence=0.85
    │ adjust_confidence_with_history() → blending 70/30 con histórico
    ▼
decide_action()
    │ OOM_KILLED + HIGH severity + confidence > 0.7 + restarts < 3/hora
    │ → ROLLOUT_RESTART
    ▼
execute_sre_action()
    │ kubectl rollout restart deployment/backend-ia -n amael-ia
    │ POST /send WhatsApp → "🔄 Reiniciando backend-ia: OOM detectado"
    ▼
store_incident() → PostgreSQL sre_incidents
    ▼
_schedule_verification() → job en 5min
    ▼
[5 minutos después]
_run_verification_job()
    │ ¿deployment healthy? SÍ → generate_postmortem() (background thread)
    │ ¿deployment healthy? NO + recent deploy (<30min) → rollout undo + notify
    │ ¿deployment healthy? NO + sin deploy reciente → notify human
```

---

## 15. Decisiones de Diseño y Trade-offs

### Grafo LangGraph cacheado vs por-request

**Decisión**: El grafo se compila UNA vez al startup. `tools_map` se inyecta en el `AgentState` por request.

**Trade-off**: No se puede cambiar la topología del grafo en caliente, pero se evita recompilar el grafo en cada request (compilación es costosa). Las tools sí cambian dinámicamente.

### OllamaLLM (completion) vs ChatOllama (chat)

**Regla en la codebase**:
- `ChatOllama`: Planner, Supervisor, Reasoning (paso REASONING del executor) — necesitan seguir instrucciones del sistema
- `OllamaLLM`: Resúmenes de documentos en ingest, fallbacks simples

**Razón**: `ChatOllama` soporta `bind_tools` (necesario para LangGraph ReAct). Las instrucciones en `SystemMessage` tienen más peso que instrucciones dentro de un prompt de completion.

### Colecciones Qdrant per-usuario vs colección global con metadata

**Decisión**: Colección por usuario.

**Trade-offs**:
- Pro: aislamiento total de datos entre usuarios, fácil eliminación de datos
- Con: overhead de conexión/gestión cuando hay muchos usuarios
- Neutral: el número esperado de usuarios es pequeño (lista blanca)

### Filtrado RAG: scroll + Python vs MatchText Qdrant

**Decisión**: Scroll hasta 500 puntos + substring Python.

**Por qué no MatchText**: Requiere índice Full-Text Search en Qdrant (no configurado). Sin índice, `MatchText` es case-sensitive e ineficiente.

**Limitación**: No escala a colecciones con >500 chunks por documento filtrado. Aceptable dado el tamaño esperado de colecciones por usuario.

### Recreate vs RollingUpdate para WhatsApp Bridge

**Decisión**: `strategy: type: Recreate`

**Razón crítica**: Chromium/Puppeteer escribe un archivo `SingletonLock` en el perfil de usuario. Con RollingUpdate, el pod nuevo arranca antes de que el viejo muera — ambos pods intentan usar el mismo perfil (PVC compartido) → Chromium del pod nuevo se bloquea indefinidamente.

### Sin `from __future__ import annotations` en todo el código

**Excepción**: Los archivos con anotaciones de tipos forward-reference (como `agents/sre/`) sí lo usan, siguiendo la corrección del bug `NameError: Anomaly is not defined` de k8s-agent.

---

## 16. Limitaciones Conocidas

### Idioma de Respuesta (v1.4.5)

**Problema**: Cuando el usuario pregunta en español y el contexto RAG está completamente en inglés, qwen2.5:14b tiende a responder en inglés a pesar de la instrucción en `SystemMessage`.

**Estado**: Se implementó post-traducción via `_detect_language()` (heurística por marcadores léxicos). No completamente confiable — en algunos casos la heurística no detecta correctamente el idioma de la respuesta generada.

**Solución futura**: Cambio de modelo a uno con mejor multilingual instruction-following, o post-processing con modelo de traducción dedicado.

### RAG Cross-lingual

**Problema**: Búsqueda semántica con nomic-embed-text no es perfectamente cross-lingual. Query en español sobre documento en inglés puede no recuperar los chunks más relevantes.

**Mitigación actual**: Filtro por filename + cosine reranking.

### Single GPU / Single Ollama Pod

**Problema**: Solo hay una GPU (RTX 5070) asignada a Ollama. No hay concurrencia real de requests LLM — todos los requests LLM son serializados por Ollama.

**Impacto**: Latencia alta bajo carga concurrente. Aceptable para uso personal/pequeño equipo.

### WhatsApp Session Persistence

**Problema**: Si el PVC `whatsapp-session-pvc` se corrompe o pierde, hay que re-escanear el QR de WhatsApp Web.

**Mitigación**: Backup manual periódico del contenido del PVC.

### Sin Tests Automatizados

El directorio `tests/` existe con estructura pero sin tests implementados. Las verificaciones son manuales vía peticiones HTTP directas o mensajes WhatsApp.

---

## 17. Roadmap

### Phase 6 — Observability & Polish (pendiente)

| Item | Descripción | Prioridad |
|------|-------------|-----------|
| Dashboard Grafana amael-agentic-backend | RAG hits/miss, REASONING latency, plan step distribution | Alta |
| `GET /api/documents` | Listar documentos indexados del usuario (frontend) | Alta |
| Health checks granulares | `/health/ready` que verifique Qdrant, Postgres, Redis, Ollama por separado | Media |
| Idioma de respuesta | Solución definitiva — cambio de modelo o pipeline de traducción robusto | Media |
| Tests unitarios | PlannerAgent, RAG retriever, security validator | Baja |

### Mejoras Futuras Identificadas

| Mejora | Descripción |
|--------|-------------|
| Multi-replica | Escalar amael-agentic-backend a 2+ replicas (requiere Redis session store para estado) |
| Modelo multilingual | Reemplazar qwen2.5:14b por un modelo con mejor soporte multilingual |
| Qdrant FTS index | Habilitar índice Full-Text Search para filtrado de filename sin scroll completo |
| Streaming LLM | Token-by-token streaming desde Ollama (actualmente el streaming es post-procesado word-split) |
| `GET /api/documents/search` | Búsqueda de documentos por nombre desde el frontend |
| Auto-tag documentos | Categorización automática de documentos al ingesta (tipo, tema, entidad) |
| Feedback explícito | Botón 👍/👎 en el frontend → alimenta el pipeline de mejora del supervisor |

---

## Apéndice A: Variables de Entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://ollama-service:11434` | URL del servicio Ollama |
| `LLM_MODEL` | `qwen2.5:14b` | Modelo LLM principal |
| `LLM_EMBED_MODEL` | `nomic-embed-text` | Modelo de embeddings |
| `QDRANT_URL` | `http://qdrant-service:6333` | URL de Qdrant |
| `POSTGRES_HOST` | `postgres-service` | Host PostgreSQL |
| `REDIS_HOST` | `redis-service` | Host Redis |
| `MINIO_ENDPOINT` | `minio-service:9000` | Endpoint MinIO |
| `JWT_SECRET_KEY` | (requerido) | Clave para JWTs |
| `INTERNAL_API_SECRET` | (requerido) | Secret servicios internos |
| `GOOGLE_CLIENT_ID` | (opcional) | OAuth Google |
| `GOOGLE_CLIENT_SECRET` | (opcional) | OAuth Google |
| `ALLOWED_EMAILS_CSV` | `""` | Whitelist emails |
| `K8S_ALLOWED_USERS_CSV` | `""` | Whitelist K8S_TOOL |
| `RATE_LIMIT_MAX` | `15` | Requests máximos |
| `RATE_LIMIT_WINDOW` | `60` | Ventana en segundos |
| `ENVIRONMENT` | `production` | `production` / `development` |
| `LOG_LEVEL` | `INFO` | Nivel de logging |

## Apéndice B: Procedimientos Operativos

### Desplegar nueva versión del backend

```bash
# 1. Build
docker build -t registry.richardx.dev/amael-agentic-backend:<version> .

# 2. Push
docker push registry.richardx.dev/amael-agentic-backend:<version>

# 3. Actualizar manifest (NUNCA kubectl set image)
sed -i 's|amael-agentic-backend:<old>|amael-agentic-backend:<version>|' \
  k8s/agents/05-backend-deployment.yaml

# 4. Aplicar
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia
```

### Regenerar JWT del bot WhatsApp

```bash
# Obtener JWT_SECRET_KEY del pod
SECRET=$(kubectl exec -n amael-ia deploy/amael-agentic-deployment -- \
  printenv JWT_SECRET_KEY)

# Generar nuevo token
TOKEN=$(kubectl exec -n amael-ia deploy/amael-agentic-deployment -- \
  python3 -c "
from jose import jwt
token = jwt.encode({'sub': 'bot-amael@richardx.dev'}, '$SECRET', algorithm='HS256')
print(token)
")

# Patch del secret
kubectl patch secret amael-secrets -n amael-ia --type='json' \
  -p='[{"op":"replace","path":"/data/jwt-token","value":"'$(echo -n $TOKEN | base64)'"}]'

# Restart whatsapp-bridge para recargar el token
kubectl rollout restart deployment/whatsapp-bridge-deployment -n amael-ia
```

### Fix Chromium SingletonLock (WhatsApp bridge colgado)

```bash
kubectl scale deployment whatsapp-bridge-deployment -n amael-ia --replicas=0

kubectl run fix-lock --image=busybox --restart=Never -n amael-ia \
  --overrides='{"spec":{"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"whatsapp-session-pvc"}}],"containers":[{"name":"fix","image":"busybox","command":["sh","-c","rm -f /data/WWebJS/Default/SingletonLock /data/WWebJS/Default/SingletonCookie && echo done"],"volumeMounts":[{"name":"v","mountPath":"/data"}]}]}}'

kubectl logs fix-lock -n amael-ia  # verificar "done"
kubectl delete pod fix-lock -n amael-ia

kubectl scale deployment whatsapp-bridge-deployment -n amael-ia --replicas=1
```

### Restart Ollama (requerido tras actualización de modelo)

```bash
# NO usar rollout restart — el nuevo pod quedaría Pending esperando la GPU
kubectl delete pod -l app=ollama -n amael-ia
kubectl wait --for=condition=ready pod -l app=ollama -n amael-ia --timeout=120s
```

---

*Documento generado automáticamente a partir del código fuente — 2026-03-15*
