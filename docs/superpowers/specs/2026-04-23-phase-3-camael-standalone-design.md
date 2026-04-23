# Fase 3 — Camael Standalone (design spec)

**Fecha:** 2026-04-23
**Autor:** Ricardo + Claude
**Precursor:** `docs/PHASE0-ANALYSIS.md` (análisis completo del split Raphael/Camael)
**Estado previo:** Fase 2 cerrada. Raphael corre en `raphael-service:8002`. Camael sigue embebido en el backend **y duplicado en raphael-service** (ambas imágenes importan `agents/devops/*`).

---

## 1. Objetivo

Cerrar el split de agentes llevando a Camael a su propio pod `camael-service:8003`. Al final de Fase 3:

- Camael corre aislado en su propio deployment
- Raphael y backend delegan a Camael vía HTTP cuando `CAMAEL_MODE=remote`
- Ninguno de los dos importa `agents/devops/*` en tiempo de ejecución cuando el flag está activo
- Un solo agente es dueño de Bitbucket + ServiceNow (Camael)
- Canary independiente del de Raphael: `AGENTS_MODE` y `CAMAEL_MODE` son flags separados

## 2. Decisiones tomadas en brainstorming

| Decisión | Elegida | Motivo |
|---|---|---|
| Topología de Camael | **A** — pod propio `camael-service` | Cierra el split completo, misma arquitectura que Raphael, canary independiente |
| Feature flag | **B** — `CAMAEL_MODE` separado de `AGENTS_MODE` | Permite combinaciones (Raphael remoto + Camael inprocess), rollback granular |
| Transporte handoff | **B** — HTTP async + Redis WAL | Raphael no bloquea su loop de 60s; Camael no pierde eventos si rebota |
| Coupling `healer.py:807` | **A** — Raphael delega vía HTTP a Camael | Camael = único dueño de ServiceNow; Raphael queda puro (cluster ops) |

## 3. Arquitectura objetivo

```
┌─────────────────────┐  observa       ┌──────────────┐
│  raphael-service    │◄──────────────►│ k8s + Prom   │
│  :8002              │                └──────────────┘
│  ─────────────      │
│  - SRE loop         │  POST /handoff     ┌──────────────────┐
│  - Verif post-deploy├───────────────────►│  camael-service  │
│  - Redis WAL        │  PATCH /rfc/{id}   │  :8003           │
│    producer         │                    │  ─────────────   │
└─────────────────────┘                    │  - GitOps PR     │
          │                                │  - ServiceNow    │
          │ fallback si Camael caído       │    RFC lifecycle │
          ▼                                │  - WAL consumer  │
    ┌──────────┐                           │    drain start   │
    │  Redis   │◄──────── lee queue ───────┤                  │
    │  WAL     │                           └──────────────────┘
    │  queue   │                                   ▲
    └──────────┘                                   │ HTTP (CAMAEL_MODE=remote)
                                           ┌───────┴──────────┐
                                           │  backend         │
                                           │  /api/devops/*   │
                                           │  → clients.camael│
                                           │     _client      │
                                           └──────────────────┘
```

### Invariantes

- Camael = único dueño de Bitbucket + ServiceNow post-3.6
- Raphael habla solo con k8s, Prometheus, Redis, camael-service (HTTP)
- Backend no carga código de Camael cuando `CAMAEL_MODE=remote`
- Redis WAL garantiza entrega de handoffs y `rfc_update` si Camael está caído
- Canary independiente — `CAMAEL_MODE=inprocess` mientras `AGENTS_MODE=remote` es el estado seguro durante el rollout

### Rollback

```bash
kubectl set env deployment/amael-agentic-deployment -n amael-ia CAMAEL_MODE=inprocess
kubectl set env deployment/raphael-service        -n amael-ia CAMAEL_MODE=inprocess
kubectl scale deployment/camael-service           -n amael-ia --replicas=0
```

Handoffs pendientes en `wal:camael:*` los drena el backend o raphael al siguiente arranque inprocess.

## 4. Componentes

### Código nuevo

| Componente | Ubicación | Propósito |
|---|---|---|
| `camael_service/main.py` | nuevo | Entry point FastAPI análogo a `raphael_service/main.py`. Lifespan dispara drain del WAL al arrancar. Puerto `:8003`. Reutiliza imagen del monorepo |
| `clients/camael_client.py` | nuevo | Dispatcher: `CAMAEL_MODE=remote` → HTTP a `camael-service:8003`; `inprocess` → import directo. Expone `handoff(anomaly, incident_key, notifier)` y `update_rfc(sys_id, result, message)` |
| `storage/redis/wal.py` | nuevo | WAL genérico. `enqueue(topic, key, payload)` / `drain(topic, consumer_fn)`. Topics: `wal:camael:handoff`, `wal:camael:rfc_update`. TTL 24h. Idempotencia por `key` |
| `docs/openapi/camael-service.yaml` | nuevo | Contrato: `POST /api/devops/handoff`, `PATCH /api/devops/rfc/{sys_id}`, más todos los `/api/devops/*` que ya existen en el backend |
| `k8s/agents/1X-camael-service-*.yaml` | nuevo | Deployment, Service, SA, Role+RoleBinding (acceso a secretos Bitbucket/SN), NetworkPolicy, Vault role `camael-service` |

### Código modificado

| Archivo | Cambio |
|---|---|
| `config/settings.py` | `camael_mode: Literal["inprocess","remote"] = "inprocess"`, `camael_service_url: str = "http://camael-service:8003"` |
| `agents/sre/healer.py:807` | `from agents.devops import servicenow_client` → `from clients.camael_client import update_rfc`. Fallback WAL en excepción |
| `agents/sre/scheduler.py:420` | `healer.handoff_to_camael(...)` → `camael_client.handoff(...)`. Fallback WAL en excepción |
| `interfaces/api/routers/devops.py` | Todas las rutas delegan a `camael_client` (patrón igual al usado en `routers/sre.py` en Fase 2.2) |
| `main.py` (backend) | Gate: si `CAMAEL_MODE=remote` no importar `agents/devops/*` ni correr sus hooks de lifespan |

### Fuera de scope (no se toca)

- `agents/devops/*` (2,283 LoC) — se empaqueta en la misma imagen del monorepo. **camael-service siempre lo carga** (es su razón de ser). Backend y raphael-service lo cargan sólo si su `CAMAEL_MODE=inprocess`
- Postgres (`camael_gitops_actions`, `servicenow_rfcs`), Qdrant runbooks, Redis dedup (`bb:pending_pr:*`, `sre:gitops:*`) — compartidos, sin migración
- Grafana dashboards / Prometheus metric names — solo cambia label `job`

## 5. Contratos HTTP

### `POST /api/devops/handoff`

Invocado por Raphael cuando decide que una anomalía amerita acción GitOps (ROLLOUT_RESTART que probablemente necesita aumentar recursos).

Request:
```json
{
  "incident_key": "oom:amael-demo-oom:amael-ia",
  "anomaly": {
    "issue_type": "OOM_KILLED",
    "severity": "HIGH",
    "deployment": "amael-demo-oom",
    "namespace": "amael-ia",
    "confidence": 0.92,
    "context": { "...": "..." }
  },
  "source": "raphael-service"
}
```

Response `202 Accepted`:
```json
{ "accepted": true, "job_id": "camael-handoff-<uuid>" }
```

Response `409 Conflict` si `incident_key` ya está en proceso (dedup por `bb:pending_pr:{incident_key}`).

### `PATCH /api/devops/rfc/{sys_id}`

Invocado por Raphael al terminar la verificación post-deploy (T+600s).

Request:
```json
{
  "result": "closed",             // "closed" | "review"
  "deployment": "amael-demo-oom",
  "namespace": "amael-ia",
  "message": "Despliegue verificado 5min post-deploy. Healthy."
}
```

Response `200 OK`:
```json
{ "sys_id": "...", "state": "Closed", "number": "CHG0012345" }
```

### Autenticación

Bearer token `INTERNAL_API_SECRET` (mismo esquema que `raphael-service` usa hoy).

## 6. Redis WAL

### Keys

| Key | Payload | TTL |
|---|---|---|
| `wal:camael:handoff:{incident_key}` | JSON del request de handoff | 24h |
| `wal:camael:rfc_update:{sys_id}` | JSON del request de PATCH RFC | 24h |

### Productor (Raphael / backend)

```python
try:
    camael_client.handoff(...)
except (HTTPError, Timeout, ConnectionError):
    wal.enqueue("handoff", incident_key, payload)
    logger.warning("[camael_client] fallback to WAL; camael unreachable")
```

### Consumidor (camael-service)

Al arrancar (lifespan startup) y cada 5 min (APScheduler tick):
1. `SCAN` keys `wal:camael:handoff:*` y `wal:camael:rfc_update:*`
2. Para cada key: procesar payload → si éxito, `DEL key`; si error, dejar (siguiente tick reintenta)
3. Log estructurado por evento procesado

### Idempotencia

- `handoff`: ya existe dedup key `bb:pending_pr:{incident_key}` (TTL 2h)
- `rfc_update`: ServiceNow API es idempotente por `sys_id` + target state

## 7. Feature flag `CAMAEL_MODE`

Análogo a `AGENTS_MODE` pero sólo cubre Camael.

| Valor | Backend | Raphael | Pod Camael |
|---|---|---|---|
| `inprocess` (default) | carga `agents/devops/*`, expone `/api/devops/*` localmente | importa `agents/devops/*` para handoff | — |
| `remote` | no carga `agents/devops/*`; `routers/devops.py` delega via `camael_client` | delega handoff via `camael_client` | 1 pod activo |

Puntos de conmutación:
- `main.py` — lifespan gate
- `routers/devops.py` — delega al cliente
- `agents/sre/healer.py` — `update_rfc` vía cliente
- `agents/sre/scheduler.py` — `handoff` vía cliente

## 8. Descomposición en sub-fases

| Sub-fase | Alcance | Deploy | Estimado |
|---|---|---|---|
| **3.1** | OpenAPI spec + `CAMAEL_MODE` en settings + `clients/camael_client.py` skeleton + contract tests mocked | No | 30min |
| **3.2** | Backend rewire: `routers/devops.py` delega al cliente + gate en `main.py` + tests unitarios del dispatcher | No (default `inprocess`, no cambia comportamiento) | 45min |
| **3.3** | Raphael rewire: `healer.py:807` y `scheduler.py:420` via cliente + `storage/redis/wal.py` + fallback tests | No | 60min |
| **3.4** | RBAC + Vault policy + NetworkPolicy + Deployment/Service YAML (no aplicar) | No | 45min |
| **3.5** | `camael_service/main.py` scaffolding + drain WAL en startup + Dockerfile + build+push imagen `camael-service:1.0.0` | No | 45min |
| **3.6** | `kubectl apply` manifests → pod `replicas=1` → contract tests E2E → canary `CAMAEL_MODE=remote` en backend y raphael → verificación end-to-end (handoff real, RFC creado+cerrado, WAL vacío) | **Sí** | 60min |

**Total estimado:** ~4.5h repartidas en 6 commits.

### Checkpoints go/no-go

- Post-3.2: `/api/devops/*` responde igual que antes (sin observable change)
- Post-3.3: Raphael completa ciclo de observación sin errores; handoff sigue funcionando in-process
- Post-3.5: Imagen `camael-service:1.0.0` construye y arranca en local
- Post-3.6: Lease `sre-agentic-leader` sigue en raphael; camael-service procesa ≥1 handoff real; `wal:camael:*` queda vacío

## 9. Deuda que cierra Fase 3

- Coupling cross-agent `agents/sre/healer.py:807` → Camael (roto vía HTTP)
- Camael duplicado en backend + raphael-service → un solo dueño (camael-service)
- Symmetry con Raphael: ambos agentes con su propio pod, su propio Lease (si aplica), su propio canary flag

## 10. Fuera de scope

- 403 recurrente de Raphael sobre `prometheus-kube-prometheus-stack-prometheus` (deuda pre-existente)
- Warnings de `vault_knowledge.md`, `metrics_knowledge.md`, OTel `opentelemetry.instrumentation.requests`
- Nuevos dashboards Grafana para Camael (se propone en Fase 4, fuera de este spec)

## 11. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| WAL se llena si Camael cae > 24h | TTL 24h + alerta Prometheus en `redis_db_keys{pattern=~"wal:camael:.*"}` > 100 |
| Camael procesa handoff duplicado tras drain | Dedup por `bb:pending_pr:{incident_key}` ya existe, TTL 2h |
| Flag `CAMAEL_MODE=remote` con camael-service caído al momento del flip | Fallback WAL cubre; al volver Camael drena. Si Raphael/backend rearrancan en `remote` con Camael caído, handoffs se encolan |
| Rollback necesita pod Camael sano para drenar antes de flip | Rollback es `CAMAEL_MODE=inprocess` en backend/raphael — ellos mismos drenan si el flag cambia a inprocess |
| Credenciales Bitbucket/SN duplicadas en múltiples pods durante transición | Ya estaban duplicadas; Fase 3.6 no agrega exposición nueva, y Fase 5 (cleanup) las quita del backend |

## 12. Criterio de aceptación

Fase 3 se considera cerrada cuando:

1. `camael-service` pod está `1/1 Running` estable ≥24h
2. Al menos 1 handoff real completado vía HTTP (validado por log en raphael + RFC en ServiceNow)
3. Al menos 1 RFC cerrado vía `PATCH /rfc/{id}` post-verificación exitosa
4. `CAMAEL_MODE=remote` aplicado en backend y raphael
5. `redis_db_keys{pattern="wal:camael:*"} == 0` en estado estable
6. Tests de contract pasando en CI (raphael↔camael, backend↔camael)
7. Commit de cierre con bump de versión del backend (`1.11.x`)
