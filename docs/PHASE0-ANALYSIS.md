# PHASE0-ANALYSIS · Separación de Raphael y Camael a pods independientes

**Fecha:** 2026-04-22
**Autor:** Ricardo (con auditoría Claude Code)
**Branch sugerido:** `feature/agents-split`
**Estado documento:** Deliverable final Fase 0 · listo para review

---

## 0. Executive summary

### Qué encontramos

1. **Raphael vive en DOS procesos simultáneamente hoy**:
   - `agents/sre/` dentro de `amael-agentic-backend` (v2 modular, 4,784 LoC en 11 módulos). Arranca con `start_sre_loop()` en `main.py:138` y para en `stop_sre_loop()` en `main.py:183`.
   - `Amael-IA/k8s-agent/main.py` (v1 monolítico, 4,712 LoC en un solo archivo). Desplegado como pod separado `k8s-agent:5.2.0` en la misma namespace.
   - **Ambos emiten las mismas métricas Prometheus** (nombres idénticos: `amael_sre_loop_runs_total`, etc.) — riesgo de contaminación de dashboards.

2. **Camael vive dentro del backend**: `agents/devops/` (2,283 LoC en 5 módulos). No existe pod separado hoy.

3. **Handoff Raphael → Camael es in-process**: `healer.handoff_to_camael()` se llama directamente desde `scheduler.py:420` en el mismo thread del APScheduler. La CLAUDE.md principal dice "daemon thread" pero la realidad del código es llamada sincrónica.

4. **Cross-module coupling**: `agents/sre/healer.py:807` importa `servicenow_client` desde `agents/devops/` — acopla Raphael a Camael a nivel de import (no sólo de HTTP).

5. **RBAC concedido dos veces**: tanto `amael-agentic-sa` como el SA del k8s-agent tienen `sre-agent-observer` (ClusterRole) + `sre-agent-healer` (Role). Si ambos loops disparan `ROLLOUT_RESTART` al mismo tiempo, el cluster lo permite.

6. **API surface ya existe**:
   - Backend: `/api/sre/*` (13 endpoints) + `/api/devops/*` (4 endpoints).
   - k8s-agent legacy: `/api/k8s-agent`, `/api/sre/*` (duplicado), `/api/sre/deploy-hook`.
   - CI workflow llama a `k8s-agent-service:8002/api/sre/deploy-hook` — hay dependencia externa viva al legacy.

### Decisión estratégica (propuesta)

**Retirar el `k8s-agent:5.2.0` legacy y promover `agents/sre/` del backend como `raphael-service`**. Razones en §10. La versión modular tiene mejor separación de concerns, tests, y cobertura de anomalías más reciente.

### Riesgos bloqueantes resueltos en Fase 0

- ✅ Conocemos los 15 call sites externos a `agents/sre/` y los 11 a `agents/devops/`.
- ✅ Tenemos inventario de dependencias compartidas (Postgres, Redis, Qdrant, Vault, Ollama).
- ✅ Diseñamos feature flag `AGENTS_MODE` con puntos de conmutación concretos.
- ✅ Plan de rollback documentado con tiempos (<5 min por fase).

### Go / No-Go para Fase 1

**Go.** No hay hallazgos bloqueantes. Ver checklist en §13.

---

## 1. Metodología y alcance

**Alcance:** auditoría de dos directorios (`agents/sre/`, `agents/devops/`) más el pod legacy `k8s-agent`. Sin cambios de código. Sólo lectura.

**Comandos ejecutados:**
- `grep -rn "from agents.sre|from agents.devops"` en `Amael-AgenticIA/`
- `grep -rn "handoff_to_camael|camael_handoff"`
- `grep -rn "K8S_AGENT_URL|k8s-agent-service:8002"` para mapear consumers del legacy
- `wc -l` por módulo
- Lectura de `main.py`, `__init__.py`, routers de `sre.py` y `devops.py`, RBAC YAML

**Fuera de alcance Fase 0:** implementación, cambios en K8s, cambios en código, deploys. Fase 0 sólo produce este documento.

---

## 2. Inventario de código actual

### agents/sre/ (Raphael v2 — dentro del backend)

| Módulo | LoC | Rol |
|---|---:|---|
| `agent.py` | 680 | `RaphaelAgent(BaseAgent)` + `start_sre_loop` + `query_agent` (LangGraph) |
| `argocd_discovery.py` | 160 | Descubre manifests vía ArgoCD Application API (reemplaza APP_MANIFEST_MAP estático) |
| `bug_library.py` | 543 | BUG_LIBRARY: fixes conocidos (memory/CPU patches, probe delays) |
| `detector.py` | 156 | `detect_anomalies`, `correlate_anomalies` |
| `diagnoser.py` | 250 | `diagnose_with_llm`, `search_runbooks`, `adjust_confidence_with_history` |
| `healer.py` | 833 | `decide_action`, `execute_sre_action`, `rollout_restart`, `rollout_undo_deployment`, **`handoff_to_camael`** |
| `__init__.py` | 56 | Re-exports públicos |
| `models.py` | 80 | `Anomaly`, `SREAction`, `SRELoopState` dataclasses |
| `observer.py` | 980 | `observe_cluster`, `observe_metrics`, `observe_trends`, `observe_slo`, `observe_node_resources`, `observe_pvc_capacity`, `observe_certificates` (P7) |
| `reporter.py` | 331 | `store_incident`, `notify_whatsapp_sre`, `get_recent_incidents`, `get_recent_postmortems` |
| `runbook_consolidator.py` | 270 | Nivel 3 — consolidación diaria de runbooks (03:00 UTC) |
| `scheduler.py` | 458 | `sre_autonomous_loop` (APScheduler 60s), `CircuitBreaker`, `get_loop_state`, lease management |
| **TOTAL** | **4,797** | |

### agents/devops/ (Camael — dentro del backend)

| Módulo | LoC | Rol |
|---|---:|---|
| `agent.py` | 1,009 | `DevOpsAgent(BaseAgent)` — entry point Camael, orquesta BB + SN + LLM |
| `bitbucket_client.py` | 360 | `list_pipelines`, `trigger_pipeline`, `get_pipeline`, `search_file_in_repo`, `merge_pr`, `create_branch`, `create_commit`, `create_pr` |
| `camael_analyzer.py` | 354 | `analyze_and_decide` — LLM con `think=False` decide multiplier + PR title |
| `rfc_templates.py` | 237 | Templates de RFC ServiceNow (emergency, standard) |
| `servicenow_client.py` | 323 | Cliente REST ServiceNow, state machine Draft→Assess→Scheduled→Implement→Closed |
| `__init__.py` | 0 | vacío |
| **TOTAL** | **2,283** | |

### k8s-agent legacy (Raphael v1 — pod separado)

| Archivo | LoC | Rol |
|---|---:|---|
| `main.py` | 4,712 | Monolítico. Implementa el mismo loop P0-P7 con las mismas métricas. |
| `vault_knowledge.md` + `metrics_knowledge.md` | — | KB cargadas en memoria para consultas conversacionales |
| `runbooks/` | — | 15 archivos markdown (indexados también en Qdrant) |

---

## 3. Mapa de call sites (consumers externos)

### Externos a `agents/sre/` (15 call sites)

| Archivo | Línea | Qué importa | Criticidad |
|---|---:|---|---|
| `main.py` | 135 | `init_runbooks_qdrant, init_sre_db, start_sre_loop` | 🔴 (startup) |
| `main.py` | 182 | `stop_sre_loop` | 🟡 (shutdown) |
| `interfaces/api/routers/sre.py` | 48, 64, 79, 94, 108, 122, 136, 147, 198, 218 | 10× imports dinámicos (`get_loop_state`, `get_recent_incidents`, `get_recent_postmortems`, `get_historical_success_rate`, `get_slo_burn_rates`, `activate_maintenance`, `deactivate_maintenance`, `load_slo_targets`, más) | 🔴 (API pública) |
| `agents/devops/agent.py` | 40, 378, 398, 411 | `_patch_cpu_limit`, `_patch_memory_limit`, `get_fix`, `is_known_resource`, `BugFix` | 🔴 (cross-agent) |
| `tests/unit/test_bug_library.py` | 14 | múltiples símbolos | 🟢 (test) |
| `tests/unit/agents/sre/test_pod_failed_gitops.py` | 5, 6 | `_patch_cpu_limit, _patch_memory_limit, _GITOPS_FIXABLE` | 🟢 (test) |
| `tests/unit/agents/devops/test_bitbucket_discovery.py` | 8 | `is_known_resource` | 🟢 (test) |
| `tests/unit/test_chaos_experiments.py` | 475, 494 | `BUG_LIBRARY` | 🟢 (test) |

### Externos a `agents/devops/` (11 call sites)

| Archivo | Línea | Qué importa | Criticidad |
|---|---:|---|---|
| `interfaces/api/routers/devops.py` | 280, 307, 388, 412, 485, 691 | 6× imports dinámicos (`list_pipelines`, `merge_pr`, `servicenow_client`, `_BB_BASE`, `_auth`, `_headers`) | 🔴 (API pública) |
| `agents/sre/healer.py` | 807 | `servicenow_client as sn` — **acopla Raphael a Camael** | 🔴 (cross-agent) |
| `tests/unit/agents/devops/test_bitbucket_discovery.py` | 54, 67, 93, 105, 129 | `search_file_in_repo` | 🟢 (test) |
| `tests/unit/agents/sre/test_pod_failed_gitops.py` | 4 | `_ISSUE_DESCRIPTIONS` | 🟢 (test) |

### Consumers del k8s-agent legacy vía HTTP (5 call sites)

| Consumer | Archivo | Endpoint invocado |
|---|---|---|
| Backend health check | `observability/health.py:243` | `GET /health` |
| Chat router (K8S_TOOL) | `interfaces/api/routers/chat.py:429` | `POST /api/k8s-agent` |
| CI workflow | `.github/workflows/ci.yml:239` | `POST /api/sre/deploy-hook` |
| ConfigMap | `k8s/config/01-configmap.yaml:26` | — declara URL — |
| Settings | `config/settings.py:57-58` | — declara URL — |

**Implicación clave:** el legacy NO es puramente redundante. El backend lo usa como executor para queries conversacionales de Kubernetes (step type `K8S_TOOL` del pipeline LangGraph). Si lo retiramos, hay que mover esa responsabilidad a `raphael-service` nuevo o mantenerla en backend.

---

## 4. Comparativa Raphael v1 (k8s-agent legacy) vs v2 (agents/sre/)

| Dimensión | v1 legacy (`k8s-agent/main.py`) | v2 modular (`agents/sre/`) |
|---|---|---|
| **Tamaño** | 4,712 LoC monolíticas | 4,797 LoC en 11 módulos separados |
| **Testabilidad** | Sin tests dedicados visibles | 4 test suites (`test_bug_library`, `test_pod_failed_gitops`, `test_chaos_experiments`, `test_bitbucket_discovery`) |
| **Cobertura P0-P7** | ✅ Completa (según comentarios en métricas) | ✅ Completa + P7 infra proactiva (observer `node_disk/memory`, `pvc_capacity`, `certificates`) |
| **LangGraph** | `create_react_agent` + classic fallback | `RaphaelAgent(BaseAgent)` + `query_agent` integrado con orchestrator |
| **ArgoCD discovery** | Sin mencionar | `argocd_discovery.py` — reemplaza APP_MANIFEST_MAP estático |
| **Runbook consolidation** | No tiene cronjob diario | `runbook_consolidator.py` — APScheduler 03:00 UTC |
| **Handoff Camael** | No tiene Camael (pre-Camael era) | `handoff_to_camael` integrado |
| **KB cargada** | `vault_knowledge.md`, `metrics_knowledge.md` | Vía skills/tools genéricas del backend |
| **API surface** | `/api/k8s-agent` conversational + `/api/sre/*` + `/api/sre/deploy-hook` | `/api/sre/*` (13 endpoints) sin `deploy-hook` |
| **K8S_TOOL consumer** | ✅ el backend lo llama para queries conversacionales | ❌ no expone equivalente a `/api/k8s-agent` |
| **Deploy hook CI** | ✅ CI lo llama | ❌ no existe |
| **Métricas emitidas** | Mismos nombres `amael_sre_*` | Mismos nombres `amael_sre_*` |
| **Última versión** | 5.2.0 | embed en backend 1.10.94 |

### Matriz de decisión: qué hacer con el legacy

| Opción | Pros | Contras | Score |
|---|---|---|---:|
| **A. Retirar legacy, promover v2** | Una sola fuente de verdad; v2 más moderno; mejor tests | Perder `/api/k8s-agent` conversational → hay que reimplementar o mover esa ruta al backend; perder `/api/sre/deploy-hook` → migrar CI | **9/10** |
| B. Mantener ambos | Cero cambios inmediatos | Acumulación de deuda; dashboards contaminados; double-action posible | 3/10 |
| C. Retirar v2, promover legacy | Legacy ya está en pod separado | v2 tiene mejor arquitectura; acoplamiento con Camael se perdería; sin tests; menor cobertura P7 | 4/10 |
| D. Fusionar en un único raphael-service nuevo desde cero | Diseño limpio | 2-3 semanas extra; riesgo alto de regresión | 5/10 |

**Recomendación:** Opción A. Ver §10 para plan detallado.

---

## 5. Dependencias internas del backend que `agents/sre/` + `agents/devops/` usan

Al separar a pods propios, estas dependencias deben resolverse (duplicar, llamar por HTTP, o dejar en shared state).

| Dependencia | Usada por | Solución al separar |
|---|---|---|
| `config.settings` | Ambos | Copiar settings por servicio (YAML/ENV) |
| `storage.postgres.client` (pool compartido) | Ambos — lee/escribe `sre_incidents`, `camael_gitops_actions` | **Shared DB**: cada servicio abre su propio pool al mismo Postgres |
| `storage.redis.client` | Ambos — dedup, counters, handoff queue | **Shared Redis**: cada servicio su propio cliente |
| `skills.llm.client` (ChatOllama wrapper) | Ambos | Duplicar wrapper mínimo O mantener llm-adapter como intermediario |
| `skills.rag.qdrant_client` | Raphael (search_runbooks) | **Shared Qdrant**: cliente directo |
| `skills.vault.client` | Camael (BB creds, SN creds) | **Shared Vault**: cada SA con política dedicada |
| `observability.metrics` (Counters, Histograms) | Ambos | Cada servicio expone `/metrics` propio; dashboards deben distinguir por `job=` label |
| `observability.tracing.instrument_app` | Ambos | Cada servicio instrumenta OTel contra el mismo collector |
| `agents.base.BaseAgent`, `AgentContext`, `AgentResult` | Ambos | Publicar como paquete compartido `amael-core` o copiar mínimo |
| `core.constants` (`StepType`, `AnomalyType`, `Severity`) | Ambos | Paquete compartido `amael-core` |
| `tools.whatsapp` (notify) | Raphael | Raphael invoca `whatsapp-bridge` directamente vía HTTP (mismo patrón que hoy) |

**Conclusión:** no hay dependencia que obligue a mantenerlos en el backend. Todas son shared-state o pueden duplicarse como cliente mínimo.

---

## 6. Estado compartido (no se toca — cada servicio lo accede por su lado)

| Store | Tablas/Keys/Colecciones | Raphael | Camael | Backend |
|---|---|:---:|:---:|:---:|
| **PostgreSQL** | `sre_incidents` | R/W | R | R |
| | `sre_learning_stats` | R/W | — | R |
| | `sre_postmortems` | R/W | — | R |
| | `sre_slo_targets` | R/W | — | R |
| | `camael_gitops_actions` | R | R/W | R |
| | `conversations`, `messages` | — | — | R/W |
| | `user_profile`, `user_identities` | — | — | R/W |
| **Redis** | `sre:incident:*` (dedup) | R/W | — | — |
| | `sre:restarts:*` (counters) | R/W | — | — |
| | `sre:restart_limit:*` | R/W | — | — |
| | `sre:maintenance:active` | R/W | R | R |
| | `sre:gitops:*` (handoff dedup) | W | R/W | — |
| | `bb:pending_pr:*` (PR dedup 2h TTL) | — | R/W | — |
| | `rate_limit:*`, `session:*` | — | — | R/W |
| **Qdrant** | `sre_runbooks` collection | R/W | — | R |
| | `user_{email}` collections (RAG) | — | — | R/W |
| **Vault** | `secret/data/amael/google-tokens/*` | — | — | R |
| | `secret/data/amael/bitbucket/*` (propuesto nuevo path) | — | R | — |
| | `secret/data/amael/servicenow/*` (propuesto nuevo path) | — | R | — |
| | `secret/data/amael/internal-secret` (propuesto nuevo path) | R | R | R |
| **MinIO** | `amael-uploads` bucket | — | — | R/W |

---

## 7. Superficie de API actual

### Backend `/api/sre/*` (router `interfaces/api/routers/sre.py`)

| Endpoint | Método | Auth | Consumer |
|---|---|---|---|
| `/api/sre/loop/status` | GET | JWT | Frontend, Grafana annotations |
| `/api/sre/incidents` | GET | JWT | Frontend |
| `/api/sre/postmortems` | GET | JWT | Frontend |
| `/api/sre/learning/stats` | GET | JWT | Frontend |
| `/api/sre/slo/status` | GET | JWT | Frontend |
| `/api/sre/maintenance` | GET | JWT | Frontend |
| `/api/sre/maintenance` | POST | Internal Bearer | WhatsApp bridge, curl admin |
| `/api/sre/maintenance` | DELETE | Internal Bearer | WhatsApp bridge |
| `/api/sre/command` | POST | Internal Bearer | WhatsApp bridge (`/sre <cmd>`) |

### Backend `/api/devops/*` (router `interfaces/api/routers/devops.py`)

| Endpoint | Método | Auth | Consumer |
|---|---|---|---|
| `/api/devops/ci-hook` | POST | Internal Bearer | GitHub Actions post-build |
| `/api/devops/command` | POST | Internal Bearer | WhatsApp bridge |
| `/api/devops/webhook/bitbucket` | POST | Webhook signature | Bitbucket (PR approved, merged) |

### k8s-agent legacy

| Endpoint | Método | Auth | Consumer |
|---|---|---|---|
| `/api/k8s-agent` | POST | Internal Bearer | Backend chat.py (K8S_TOOL) |
| `/api/sre/*` | varios | Internal Bearer | Duplicado con backend |
| `/api/sre/deploy-hook` | POST | Internal Bearer | GitHub Actions post-deploy |
| `/api/sre/command` | POST | Internal Bearer | Duplicado con backend |
| `/health` | GET | — | Backend health check |
| `/metrics` | GET | — | Prometheus scrape |

### Endpoints nuevos requeridos en Fase 2/3

Serán diseñados en Fase 1 (OpenAPI specs):

- `raphael-service`:
  - Todos los `/api/sre/*` existentes, movidos 1:1
  - NUEVO: `/api/k8s-agent` (heredar de legacy para no romper chat K8S_TOOL)
  - NUEVO: `/api/sre/deploy-hook` (heredar de legacy)
  - NUEVO: `/api/raphael/handoff/from` (recibe catch-up desde Camael si hay replays)

- `camael-service`:
  - Todos los `/api/devops/*` existentes, movidos 1:1
  - NUEVO: `/api/camael/handoff` (recibe desde Raphael — reemplaza llamada in-process)
  - NUEVO: `/api/camael/handoff/{id}/status` (poll desde Raphael o backend)

- Backend:
  - NUEVO cliente HTTP: `clients/raphael_client.py`, `clients/camael_client.py`
  - Mantiene `/api/chat`, `/api/conversations`, etc. sin cambios

---

## 8. Contratos externos que dependen de la arquitectura actual

**Estos son los consumidores externos que pueden romperse si la migración se hace mal.**

| Consumidor | Qué llama | Cómo adaptar |
|---|---|---|
| **whatsapp-bridge** | `POST /api/sre/command` (backend:8000) | Mantener endpoint en backend, que proxy-ee a raphael-service. O reconfigurar env var `AMAEL_API_URL`. |
| **whatsapp-bridge** | `POST /api/devops/command` | Mismo patrón |
| **GitHub Actions CI** | `POST k8s-agent-service:8002/api/sre/deploy-hook` | Reapuntar a `raphael-service:8002` o mantener servicio alias |
| **Bitbucket webhook** | `POST /api/devops/webhook/bitbucket` | Proxy desde backend, o reapuntar webhook a camael-service |
| **Grafana dashboards** | Scrape de `amael_sre_*` de ambos pods | Dashboards ya existentes (`amael-sre-agent`, `amael-backend`) — verificar que filtren por `job` label tras split |
| **Grafana dashboard Camael** | Nuevo — no existe aún | Crear en Fase 4 |
| **ConfigMap `k8s-agent-configmap`** | Variables de entorno | Migrar a `raphael-service-config` nuevo CM |
| **ConfigMap `sre-agent-policy`** | Thresholds, circuit breaker | Move to raphael-service-config |
| **ConfigMap `sre-agent-slo`** | SLO_TARGETS_JSON | Move to raphael-service-config |
| **Frontend Next.js** | GET `/api/sre/*` y `/api/devops/*` | Sin cambio si el backend proxy-ea |

**Decisión de diseño clave:** ¿el backend actúa como reverse proxy transparente hacia raphael/camael, o los clientes externos llaman directo?

**Recomendación:** el backend sigue siendo el *único* endpoint externo (`amael-ia.richardx.dev`). Dentro del cluster, los routers del backend llaman a los pods por DNS (`raphael-service:8002`, `camael-service:8003`). Esto mantiene:
- Auth JWT centralizada
- CORS centralizado
- Rate limiting centralizado
- Observabilidad request→trace unificada

Los endpoints `/api/sre/*` siguen existiendo en backend, pero su implementación ahora es "hacer HTTP a raphael-service".

---

## 9. Feature flag `AGENTS_MODE`

### Diseño

```python
# config/settings.py
class Settings(BaseSettings):
    agents_mode: Literal["inprocess", "remote"] = Field(
        default="inprocess",
        alias="AGENTS_MODE",
        description="inprocess = agents/sre y agents/devops corren dentro del backend. "
                    "remote = proxying a raphael-service y camael-service vía HTTP.",
    )
    raphael_service_url: str = Field(default="http://raphael-service:8002", alias="RAPHAEL_SERVICE_URL")
    camael_service_url:  str = Field(default="http://camael-service:8003",  alias="CAMAEL_SERVICE_URL")
```

### Puntos de conmutación en el código

| Archivo | Cambio requerido |
|---|---|
| `main.py:134-141` (startup SRE) | `if settings.agents_mode == "inprocess": start_sre_loop()` |
| `main.py:181-186` (shutdown SRE) | `if settings.agents_mode == "inprocess": stop_sre_loop()` |
| `interfaces/api/routers/sre.py` (13 imports) | Wrapper: si `remote`, hacer HTTP a raphael-service; si `inprocess`, import directo |
| `interfaces/api/routers/devops.py` (6 imports) | Mismo patrón, contra camael-service |
| `agents/sre/healer.py:807` (import servicenow_client) | Wrapper: `from clients.camael_client import create_rfc` (abstrae HTTP vs in-process) |
| `agents/sre/scheduler.py:420` (handoff_to_camael) | Si `remote`: `camael_client.handoff()`; si `inprocess`: llamada directa existente |

### Estrategia de implementación

Crear capa de abstracción `clients/raphael_client.py` y `clients/camael_client.py` que:
- En modo `inprocess`: re-exporta las funciones del módulo local
- En modo `remote`: hace HTTP con httpx async + retry + circuit breaker

Los routers y el healer pasan a depender del `client`, no del módulo directo. Así el flag conmuta sin cambiar lógica de llamada.

### Cuándo se activa

- **Fase 1:** flag existe pero `AGENTS_MODE=inprocess` siempre. Sólo se valida que el código compila y los tests pasan con el wrapper nuevo.
- **Fase 2:** flag `remote` disponible; canary 10% → 50% → 100% vía rollout por réplicas.
- **Fase 3:** igual para Camael.
- **Fase 5 (cleanup):** eliminar modo `inprocess` y el flag.

---

## 10. Plan de rollback

### Criterios para disparar rollback

- Error rate backend > 5× baseline durante 10 min
- MTTR de incidentes se duplica vs baseline histórico (10min → >20min)
- Handoff success rate < 95% en 24h
- Alert pagea SRE humano por raphael-service DOWN > 5min

### Procedimiento por fase

**Fase 2 (Raphael standalone):**
1. `kubectl set env deployment/amael-agentic-deployment AGENTS_MODE=inprocess -n amael-ia` — tiempo: <30s
2. `kubectl scale deployment/raphael-service --replicas=0 -n amael-ia` (opcional — mantener pod para debugging)
3. Backend vuelve a correr `agents/sre/` in-process al siguiente rollout
4. El loop v2 retoma en el backend como antes
5. Verificar que `/api/sre/loop/status` responde y `amael_sre_loop_runs_total` incrementa
6. **Tiempo total:** <5 min

**Fase 3 (Camael standalone):**
1. `kubectl set env deployment/amael-agentic-deployment AGENTS_MODE=inprocess` — tiempo <30s
2. `kubectl scale deployment/camael-service --replicas=0`
3. Handoffs pending en Redis queue `camael:pending_handoff:*` se procesarán in-process al siguiente arranque del backend
4. **Tiempo total:** <5 min

**Fase 5 (cleanup — punto de no-retorno):**
- Se ha removido el código inprocess. Para rollback aquí hay que `git revert` + rebuild + redeploy.
- **Tiempo:** 15-30 min (build + push + deploy)
- **Mitigación:** mantener imagen anterior del backend tageada (`backend:1.10.94-inprocess`) durante 30 días.

### Datos que sobreviven al rollback

| Dato | Sobrevive? | Notas |
|---|:---:|---|
| `sre_incidents` | ✅ | Postgres compartido |
| `camael_gitops_actions` | ✅ | Postgres compartido |
| Redis dedup keys | ✅ | Mismo Redis |
| Handoffs pendientes en queue | ⚠️ | Sólo si Fase 3 había encolado — se drenan al volver a inprocess |
| Runbooks Qdrant | ✅ | Mismo Qdrant |
| Métricas Prometheus | ✅ | Mismos nombres, sólo cambia `job` label |
| Postmortems | ✅ | Postgres compartido |

Ningún dato se pierde por rollback. El flag es completamente reversible.

---

## 11. Imágenes Docker y versionado

### Propuesta

| Imagen | Repo origen | Tag inicial | Base |
|---|---|---|---|
| `registry.richardx.dev/raphael-service` | Subdirectorio `raphael-service/` nuevo dentro de Amael-AgenticIA, O repo separado `amael-raphael` | `1.0.0` | python:3.12-slim |
| `registry.richardx.dev/camael-service` | Subdirectorio `camael-service/` nuevo O repo separado `amael-camael` | `1.0.0` | python:3.12-slim |
| `registry.richardx.dev/amael-agentic-backend` | Sin cambios | `1.11.x` (bump minor al integrar clients) | existente |

### Decisión a tomar en Fase 1

¿Monorepo o repos separados?

- **Monorepo (subdirectorios en Amael-AgenticIA):** más fácil compartir código core; un solo CI; pero builds acoplados.
- **Repos separados:** dueño claro por servicio; CI independiente; permite que Luis Penagos u otro ingeniero tome Raphael sin ver el resto.

**Recomendación:** monorepo por ahora (velocidad), refactor a repos separados en Fase 5 si lo amerita.

---

## 12. Namespace y networking

### Propuesta

**Mantener todos en `amael-ia`.** Crear una nueva namespace (`amael-agents`) añade overhead de NetworkPolicies sin beneficio real — los tres servicios ya comparten dependencias (Postgres, Redis, Qdrant, Vault) que viven en `amael-ia`.

### NetworkPolicies nuevas

```yaml
# raphael-service: recibe sólo desde backend; emite a cualquier cosa del cluster
# camael-service:  recibe desde backend O raphael-service; emite a Bitbucket/ServiceNow (egress allowed)
# backend:         recibe de Ingress; emite a raphael + camael + infra
```

Diseño detallado en Fase 1.

---

## 13. Checklist Go/No-Go para Fase 1

Criterios verificables antes de empezar Fase 1:

- [x] Inventario de código completo con LoC y roles (§2)
- [x] 26 call sites externos mapeados con archivo+línea (§3)
- [x] Comparativa v1 vs v2 documentada con matriz de decisión (§4)
- [x] Dependencias internas clasificadas como "shared" o "duplicable" (§5)
- [x] Estado compartido documentado con matriz R/W por tabla/key/colección (§6)
- [x] API surface actual + endpoints nuevos identificados (§7)
- [x] Consumidores externos listados con plan de adaptación (§8)
- [x] Feature flag `AGENTS_MODE` diseñado con puntos de conmutación (§9)
- [x] Plan de rollback con tiempos (§10)
- [x] **Decisión tomada sobre legacy k8s-agent:** ✅ Retirar — ratificado 2026-04-22
- [x] **Decisión tomada sobre monorepo vs split:** ✅ Monorepo dentro de `Amael-AgenticIA/` — ratificado 2026-04-22
- [x] **Feature flag en Fase 1:** ✅ Sí — ratificado 2026-04-22
- [ ] Branch `feature/agents-split` creado
- [ ] Imagen del backend actual tageada como `backend:1.10.94-inprocess` para rollback

### Decisiones ratificadas

1. **Retirar k8s-agent legacy:** ✅ SÍ
   - `/api/k8s-agent` conversational se moverá a `raphael-service`
   - `/api/sre/deploy-hook` (CI workflow) se migrará al mismo endpoint en `raphael-service`
   - Pod `k8s-agent:5.2.0` se escala a 0 al final de Fase 2; manifest se archiva en Fase 5

2. **Monorepo dentro de `Amael-AgenticIA/`:** ✅ SÍ
   - Subdirectorios `raphael-service/` y `camael-service/` en la raíz del repo actual
   - Paquete compartido `shared/` para `BaseAgent`, `constants`, `Anomaly` dataclass
   - Un solo CI workflow que builda las 3 imágenes
   - ArgoCD sigue sincronizando desde el mismo repo/rama
   - Re-evaluar split a repos separados en Fase 5 si se suma un segundo ingeniero con ownership de un servicio

3. **Feature flag `AGENTS_MODE` creado en Fase 1:** ✅ SÍ
   - Implementado en Fase 1 pero siempre `inprocess`
   - Activado en Fase 2 (Raphael) y Fase 3 (Camael) con canary

---

## 14. Artefactos entregables de Fase 0

- ✅ **Este documento** (`docs/PHASE0-ANALYSIS.md`)
- ⏸️ Branch `feature/agents-split` (crear tras aprobar este documento)
- ⏸️ Imagen backup `backend:1.10.94-inprocess` (tag antes de Fase 1)

---

## 15. Siguiente paso — Fase 1

Si este análisis se aprueba, Fase 1 produce:

1. OpenAPI specs: `raphael-service.yaml`, `camael-service.yaml`
2. Manifests RBAC: `raphael-sa`, `camael-sa` con ClusterRoles/Roles específicos
3. Policies Vault granulares (path `secret/data/amael/bitbucket/*` sólo para camael-sa)
4. NetworkPolicies
5. Feature flag `AGENTS_MODE` implementado (pero siempre `inprocess`)
6. Layer `clients/raphael_client.py` y `clients/camael_client.py` (abstracción HTTP vs import)
7. Contract tests ejecutándose en CI

**Estimación Fase 1:** 3-5 días.

---

## Firmas

| Rol | Nombre | Fecha | Decisión |
|---|---|---|---|
| Owner técnico | Ricardo | — | Pendiente |
| Revisor Kubernetes | Luis Penagos | — | Pendiente |
| Revisor arquitectura | — | — | — |

---

*Documento generado en Fase 0 · sin cambios de código · sólo auditoría y diseño.*
