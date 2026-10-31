# CLAUDE.md — Amael-AgenticIA

Backend de **Amael IA** (`amael-agentic-backend`, namespace `amael-ia`) más los servicios que
salen de este mismo repo: **Raphael** (SRE autónomo), **Camael** (GitOps), CosyVoice, piper,
watchdog, morning-audio y el runner de GitHub. Pipeline LangGraph Planner → Grouper →
Executor → Supervisor con agentes directos (Cassiel, Haniel, Zaphkiel, recepcionista…).

Lo de todo el cluster (nodo, GPU, política de imágenes, versiones vigentes, Vault sellado)
está en `~/k8s-lab/CLAUDE.md`. Historial por versión con el porqué de cada cambio:
`~/obsidian-vault/amael/historial-versiones.md`.

---

## Build & Deploy

**El backend, raphael y camael se despliegan por push a `main`** (GitHub Actions en el runner
self-hosted: scans → tests → build & push → deploy → hook a Raphael). El CI lee la versión
del manifest: **todo push a main lleva bump** o re-empuja el tag vigente con código nuevo.

A mano (servicios fuera del CI o emergencia):

```bash
# subir primero la versión en el manifest
docker build -t registry.richardx.dev/amael-agentic-backend:<version> .
docker push registry.richardx.dev/amael-agentic-backend:<version>
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia
```

### Build por servicio — `APP_MODULE` / `APP_PORT`

Un solo `Dockerfile` publica varias imágenes. Sin los build args la imagen arranca
`main:app` en 8000 → liveness en `connection refused` → CrashLoop.

| Imagen | `APP_MODULE` | `APP_PORT` |
|--------|--------------|-----------|
| `amael-agentic-backend` | `main:app` (default) | `8000` (default) |
| `raphael-service` | `raphael_service.main:app` | `8002` |
| `camael-service` | `camael_service.main:app` | `8003` |

```bash
docker build --build-arg APP_MODULE=raphael_service.main:app --build-arg APP_PORT=8002 \
  -t registry.richardx.dev/raphael-service:<version> .
docker inspect registry.richardx.dev/raphael-service:<version> --format '{{.Config.Cmd}}'  # verificar SIEMPRE
```

**Nunca repushees un tag publicado** (`IfNotPresent`): `raphael-service:1.1.11` quedó
contaminado así.

**`k8s/agents/16-trader-deployment.yaml` es legado (trader 1.0.18)**: el trader vive en su
propio repo `~/k8s-lab/trader-service`. Aplicar ese archivo haría downgrade.

---

## Development

```bash
pip install -e ".[all]"           # apscheduler vive en el extra sre; los tests necesitan .[all]
pytest tests/                     # asyncio_mode=auto
pytest -m "not e2e" tests/
ruff check . --fix                # line-length 100, py3.11, E/F/I/UP
```

FastAPI 0.141 esconde rutas de `include_router` en `app.routes`: en tests, afirmar contra
`app.openapi()`.

---

## Architecture

### Request Flow

```
POST /api/chat
  → JWT auth → rate limit (Redis, 15/60s) → validate_prompt()
  → AgentRouter.route()          # reglas por keyword (orden importa) o LLM fallback
  → AgentDispatcher.dispatch()
       ├─ Directo: sre, productivity, research, memory (Zaphkiel), reminder/tareas (Cassiel),
       │           calendar_create (Haniel)
       └─ Pipeline (general/k8s/monitoring…) → run_workflow() → LangGraph
  → sanitize_output() → ChatResponse
```

`chat.py` antepone perfil/memoria + «[Pregunta actual]» a lo que recibe el agente:
**las pruebas de un agente directo usan el texto tal como llega desde `chat.py`**, no la
pregunta limpia.

### LangGraph Pipeline

- **Planner** (`agents/planner/agent.py`): plan JSON, `MAX_PLAN_STEPS=8`
- **Grouper**: agrupa pasos no-REASONING consecutivos en lotes paralelos
- **Executor** (`agents/executor/`): herramientas en paralelo, REASONING secuencial
- **Supervisor**: califica 0–10, REPLAN (máx. 1) si < 6

El grafo se compila una vez (`orchestration/workflow_engine.py`); `tools_map` entra por
`AgentState` en cada petición.

### Agentes directos relevantes

| Agente | Módulo | Qué hace |
|---|---|---|
| Cassiel | `agents/scheduler/` | recordatorios (`user_jobs`), pendientes (`user_tasks`), brief diario, análisis nocturno 03:45 |
| Haniel | `agents/productivity/` | Calendar/Gmail vía Vault OAuth; `event_extractor.py`, `commitment_extractor.py` |
| Zaphkiel | `agents/memory_agent/` | memoria episódica + consolidación 03:30 en hechos estables |
| Recepcionista | `agents/reception/` | números no registrados → leads (`leads`, `lead_messages`), `/lead` |
| English coach | `agents/english/grader.py` | califica respuestas citando la lección `EN-AAAA-MM-DD` |
| SRE (Raphael) | `agents/sre/` | ver abajo |

### Registries y abstracciones

`AgentRegistry` (`agents/base/agent_registry.py`), `SkillRegistry` (`skills/registry.py`),
`ToolRegistry` (`tools/registry.py`), todos singleton + decorador, poblados en el lifespan.
`core/agent_base.py` (`BaseAgent`, `AgentContext`, `AgentResult`), `core/constants.py`.

---

## Storage

| Store | Uso | Módulo |
|---|---|---|
| PostgreSQL | conversaciones, mensajes, incidentes SRE, user_jobs/tasks, leads | `storage/postgres/client.py` (schema en `_ensure_schema()`) |
| Redis | rate limit, dedup SRE, mantenimiento, followups, locks de jobs | `storage/redis/client.py` |
| Qdrant | RAG por usuario, `sre_runbooks`, `agent_skills` | `qdrant_client` — usar `query_points()`; upsert es `PUT /points` |
| MinIO | respaldo de documentos (`amael-uploads`) | `minio` |

RAG (`agents/researcher/rag_retriever.py`): filtro por filename con `client.scroll(limit=500)`
+ substring en Python (no `MatchText`, no hay índice FTS), rerank por coseno.

## Security

`security/validator.py` (4000 chars, inyección), `security/sanitizer.py` (redacta `hvs.*`,
JWTs, `password=`). Endpoints internos: `require_internal_secret()`. El bot
`bot-amael@richardx.dev` puede mandar `user_id` en el body para actuar como el usuario real.
JWT con **PyJWT** (python-jose se retiró en 1.18.5). Regenerar el token del bot:

```bash
kubectl exec -it deploy/amael-agentic-deployment -n amael-ia -- python3 -c "
import os, jwt; print(jwt.encode({'sub':'bot-amael@richardx.dev'}, os.environ['JWT_SECRET_KEY'], algorithm='HS256'))"
# luego patch de Secret amael-secrets, key jwt-token (base64)
```

## Key Env Vars

`LLM_MODEL` / `LLM_MODEL_FAST` = `qwen3.5:9b` · `LLM_VISION_MODEL` (también el 9b) ·
`OLLAMA_EMBED_URL` (ollama-cpu, embeddings) · `OLLAMA_DEEP_URL` / `LLM_DEEP_MODEL` (tier
profundo en CPU) · `RAPHAEL_SERVICE_URL` · `INTERNAL_API_SECRET` · `JWT_SECRET_KEY` ·
`WHISPER_LANGUAGE=es,en` · `COSYVOICE_EN_INSTRUCT`. Lista completa: `config/settings.py`.
**Ningún cliente manda `num_ctx`**: el contexto lo fija el servidor de Ollama.

---

## Raphael — SRE autónomo (`raphael-service:8002`, código en `agents/sre/`)

Loop APScheduler cada 60 s: lease → circuit breaker → ventana de mantenimiento →
observar (pods, métricas Prometheus, tendencias, SLO, nodo, PVC, certificados, VRAM) →
detectar → correlacionar → diagnosticar (LLM + fallback determinista + runbooks/skills de
Qdrant) → decidir → actuar (`ROLLOUT_RESTART`, `DELETE_STUCK_POD`, `NOTIFY_HUMAN`) →
guardar incidente → verificar 5 min después (`SRE_VERIFICATION_DELAY`=300) con auto-rollback
si hubo deploy en los últimos 30 min.

- **Auto-remedia**: CRASH_LOOP, OOM_KILLED, POD_FAILED, HIGH_RESTARTS, HIGH_MEMORY,
  MEMORY_LEAK_PREDICTED (rollout restart) y POD_REJECTED (borrar si tiene controlador).
- **Solo avisa**: IMAGE_PULL_ERROR, POD_PENDING_STUCK, NODE_*, HIGH_CPU, HIGH_ERROR_RATE,
  DISK/PVC/CERT, SLO_BUDGET_BURNING, LLM_MODEL_OFF_GPU, y fallas de red EXTERNA en logs.
- **Ráfagas de reinicio** (≥5 pods en ≥3 namespaces en 15 min, típico ~01:30 por la malla
  WiFi) bajan a LOW sin WhatsApp.
- **Alertas CRITICAL ≥10 min** también en voz (inglés, voz de Ricardo), fuera de 23–7 MX.
- **Skills** (`agent_skills` en Qdrant): los agentes solo PROPONEN; activar es humano con
  `/sre skill approve|reject <nombre>`.
- **Runbooks**: 18 `.md` en `runbooks/` + generados (post-incidente, bootstrap) +
  consolidación diaria 03:00 MX que fusiona en vez de reemplazar.
- **Modo sombra** (`agents/sre/shadow.py`, `SRE_SHADOW_HIGH_RESTARTS`): en HIGH_RESTARTS guarda
  en `sre_shadow_decisions` qué haría con evidencia del pod. Solo mide: no actúa ni notifica.
- **Comandos WhatsApp** `/sre <cmd>` → `POST /api/sre/command`: estado, status, incidents,
  postmortems, slo, grafana, maintenance on <min>|off, skills, ayuda. `/estado` es alias.
- **RBAC**: ClusterRole `sre-agent-observer` (lectura) + Role `sre-agent-healer` (amael-ia).

Gotchas:
- Anomalía correlacionada = Deployment: lo que opere sobre pods lee `metadata["pod_names"]`.
- El dueño de un pod sale de `ownerReferences`; nunca reescribirlo por `APP_MANIFEST_MAP`.
- `sre:maintenance:active` en Redis apaga el loop entero (revisar `ttl`).
- `CAMAEL_MODE=remote` en raphael; teléfono: `OWNER_PHONE` → `SRE_ALERT_PHONE` → `ADMIN_PHONE`.
- El postmortem va al 9b en GPU con `POSTMORTEM_TIMEOUT_S`=120 gobernando cliente y espera.

## Watchdog (`k8s/agents/17-watchdog-cronjob.yaml`, `agents/sre/watchdog.py`)

Cada 10 min, **independiente de Raphael** (sin LLM, SA de solo lectura). Avisa por WhatsApp
directo al bridge si: un deployment crítico queda sin réplicas (ollama, postgres, redis,
qdrant, backend, raphael, bridge), Vault sellado, PAT de GitHub muerto o por vencer (7 días),
backups sin éxito en 26 h, job self-hosted en cola > 20 min, o la IP de salida deja de ser
Infinitum > 15 min. Dedup en Redis 60 min, marcado solo si el aviso salió.

```bash
kubectl create job watchdog-manual --from=cronjob/amael-watchdog -n amael-ia
```

## Camael — GitOps (`camael-service`)

Raphael hace `ROLLOUT_RESTART` → handoff a Camael: lee el YAML en GitHub, el LLM elige
multiplicador de memoria, abre rama + PR y RFC en ServiceNow; verificación a los 600 s
cierra o revierte. `agents/devops/scm.py` elige proveedor (`SCM_PROVIDER=github`). GitHub
exige `sha` al actualizar un archivo y no deja auto-aprobar PRs. El PAT es el del runner
(`github-runner-secret/ACCESS_TOKEN`).

Demos (`k8s/agents/06-09-demo-*.yaml`, en GitHub con `replicas: 0`): OOM, CrashLoop,
HIGH_MEMORY, Degraded. Reset: borrar en Redis `sre:incident:*`, `sre:gitops:*`,
`sre:restarts:*`, `sre:restart_limit:*`, `bb:pending_pr:*`; volver los manifests a su estado
roto en GitHub; `kubectl apply` + borrar pods `demo=true`.

## Otros CronJobs de este repo

- **Brief diario** (`07-day-planner-cronjob.yaml`): `POST /api/planner/daily` L–V 7:00
  `America/Mexico_City` (con `timeZone`, el cron va en hora local). Catch-up L–V 7:10–11:59.
- **morning-audio** (código en `Amael-IA/morning-audio/`): guardia nocturna en voz.
- **conversation-pruning**, **english-coach** (código en `Amael-IA/english-coach`).

## POC — `https://amael-ia.richardx.dev/poc`

nginx + ConfigMap (`k8s/ingress/07-poc-presentation.yaml`). **`index.html` =
`poc-landing.html`**, NO `poc-presentation.html` (el 11-oct-2026 se publicó la vieja). El
mismo CM sirve `/privacy` y `/terms` (OAuth de Google): omitirlas las tumba.

```bash
kubectl create configmap poc-presentation-html -n amael-ia \
  --from-file=index.html=$HOME/k8s-lab/docs/POC/poc-landing.html \
  --from-file=privacy.html=$HOME/k8s-lab/docs/legal/privacy.html \
  --from-file=terms.html=$HOME/k8s-lab/docs/legal/terms.html \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/poc-presentation -n amael-ia
```

---

## Reglas aprendidas

Cada una nació de un incidente real (detalle en el historial del vault).

- **El LLM traduce intención; el código decide.** Fechas, comparaciones numéricas, matches
  de tareas, validación de planes: en código. El 9b alucina comparaciones.
- **Toda re-pregunta con LLM lleva tope de rondas en código.**
- **Nunca citar verbatim en un prompt lo prohibido**: el 9b lo repite.
- **El prompt de un job a veces ES el mensaje**, no una consulta ejecutable.
- **Grounding**: si el LLM propone cerrar/cancelar algo, el id o una palabra del título debe
  estar en las palabras del usuario (sin los bloques de contexto).
- **Un aviso que pide decisión humana nunca lleva una severidad que el filtro descarta.**
- **Un timeout de cliente y uno de espera nunca se escriben por separado.**
- **Nada bloqueante en el event loop**: requests/ffmpeg/health checks con `asyncio.to_thread`.
- **Errores que se tragan con `logger.debug` esconden agentes caídos**: Zaphkiel estuvo
  muerto meses por un `TypeError` silenciado.
- **`getattr(settings, "x", default)` nunca truena**: verificar el nombre real del campo.
- **Un lock Redis SET NX** para todo cron dentro del backend (HPA = 2 réplicas).
- **LangChain 1.2+**: nada de `langchain.agents` (`AgentType`, `initialize_agent` ya no
  existen). `Tool` sale de `langchain_core.tools`; agentes con `langgraph.prebuilt.create_react_agent`.
