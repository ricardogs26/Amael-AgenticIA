# Amael-AgenticIA

Plataforma multi-agente modular basada en LangGraph, FastAPI y Ollama para automatización inteligente en entornos empresariales. Corre como `amael-agentic-backend` en Kubernetes (MicroK8s, single-node, GPU RTX 5070).

**Versión actual:** `1.10.24`

---

## Arquitectura del pipeline

```
POST /api/chat
  → JWT auth → rate limit (Redis) → validate_prompt()
  → AgentRouter.route()
  → AgentDispatcher.dispatch()
       ├─ Directo: sre / productivity / research
       └─ Pipeline LangGraph:
            planner → grouper → batch_executor (loop) → supervisor
                ↑                                            │
                └──────── REPLAN (max 1 retry) ─────────────┘
```

---

## Agentes

| Agente | Estado | Descripción |
|--------|--------|-------------|
| `planner` | ✅ Producción | Descompone requests en planes ejecutables (max 8 pasos) |
| `executor` | ✅ Producción | Ejecuta pasos en paralelo con ThreadPoolExecutor |
| `supervisor` | ✅ Producción | Evalúa calidad 0-10, decide ACCEPT/REPLAN |
| `researcher` | ✅ Producción | RAG sobre documentos del usuario + DuckDuckGo |
| `productivity` | ✅ Producción | Google Calendar / Gmail via OAuth + Vault |
| `sre` | ✅ Producción | Loop autónomo 60s: Observe → Detect → Diagnose → Decide → Act |
| `coder` | Roadmap | Generación y refactor de código (Gabriel) |
| `devops` | Roadmap | CI/CD, pipelines |
| `qa` | Roadmap | Validación y pruebas |

---

## SRE Agent — Observabilidad completa

El agente SRE implementa **6 capas de observación** ejecutadas cada 60 segundos:

| Capa | Función | Anomalías detectadas |
|------|---------|---------------------|
| `observe_cluster()` | Pods y nodos vía K8s API | CRASH_LOOP, OOM_KILLED, IMAGE_PULL_ERROR, POD_FAILED, POD_PENDING_STUCK, HIGH_RESTARTS, NODE_NOT_READY |
| `observe_infrastructure()` | Infraestructura K8s completa | LOADBALANCER_NO_IP, SERVICE_NO_ENDPOINTS, PVC_PENDING, PVC_MOUNT_ERROR, DEPLOYMENT_DEGRADED, NODE_PRESSURE, K8S_EVENT_WARNING, VAULT_SEALED |
| `observe_metrics()` | Prometheus CPU/memoria/errores | HIGH_CPU, HIGH_MEMORY, HIGH_ERROR_RATE |
| `observe_trends()` | Predicción con predict_linear + deriv | DISK_EXHAUSTION_PREDICTED, MEMORY_LEAK_PREDICTED, ERROR_RATE_ESCALATING |
| `observe_slo()` | Error budget burn rate | SLO_BUDGET_BURNING |
| Vault health check | HTTP `/v1/sys/health` | VAULT_SEALED |

**Acciones autónomas:**
- `ROLLOUT_RESTART` — CRASH_LOOP, OOM_KILLED, POD_FAILED, HIGH_RESTARTS, HIGH_MEMORY, MEMORY_LEAK_PREDICTED, DEPLOYMENT_DEGRADED
- `ROLLOUT_UNDO` — auto-rollback si verificación post-restart falla y hay deploy reciente (< 30 min)
- `NOTIFY_HUMAN` — resto de anomalías (WhatsApp + PostgreSQL)

**Runbooks indexados en Qdrant:** 15 archivos markdown en `runbooks/`

---

## Skills y Tools

| Nombre | Tipo | Estado |
|--------|------|--------|
| `kubernetes` | Skill | ✅ |
| `rag` | Skill | ✅ |
| `llm` | Skill | ✅ |
| `vault` | Skill | ✅ |
| `web` | Skill | ✅ |
| `whatsapp` | Tool | ✅ |
| `grafana` | Tool | ✅ |
| `github` | Tool | ✅ |
| `piper` | Tool | ✅ (TTS) |
| `cosyvoice` | Tool | ✅ (TTS) |

---

## Capacidades multimedia

- **Audio (WhatsApp):** Transcripción de notas de voz con `faster-whisper` (`base`, CPU, int8). Modelo persistido en PVC `whisper-cache-pvc` (1Gi).
- **Visión:** Análisis de imágenes con `qwen2.5vl:3b` via Ollama native API. Se activa cuando el mensaje incluye `image` (base64).

---

## Stack tecnológico

| Capa | Tecnología |
|------|-----------|
| Orquestación | LangGraph StateGraph |
| LLM | Ollama — `qwen2.5:14b` (chat), `nomic-embed-text` (embeddings), `qwen2.5vl:3b` (visión) |
| API | FastAPI + SSE streaming |
| Storage | PostgreSQL · Redis · Qdrant · MinIO |
| Infraestructura | Kubernetes MicroK8s · GPU RTX 5070 · MetalLB · NGINX Ingress · cert-manager |
| Secretos | HashiCorp Vault (Shamir 3-of-5, Kubernetes Auth) |
| Observabilidad | Prometheus · Grafana (9 dashboards) · OpenTelemetry · Tempo |
| Audio | faster-whisper (base, CPU, int8) |

---

## Grafana Dashboards (9)

| # | UID | Descripción |
|---|-----|-------------|
| 1 | `amael-llm` | LLM & HTTP — latencia, tokens, throughput |
| 2 | `amael-agent` | Pipeline de Agente — pasos, herramientas, REPLAN rate |
| 3 | `amael-rag` | RAG Performance — hit/miss, latencia de búsqueda |
| 4 | `amael-infra` | Infraestructura & GPU — VRAM, CPU, pods |
| 5 | `amael-supervisor` | Supervisor & Calidad — quality scores, accept/replan |
| 6 | `amael-security` | Seguridad & Rate Limiting — blocks, rate limits |
| 7 | `amael-service-map` | Service Map — topología OTel en tiempo real |
| 8 | `amael-sre-agent` | SRE Autónomo — loop runs, acciones, confianza |
| 9 | `amael-backend` | Backend Overview — golden signals, RAG, REASONING |

---

## Despliegue

```bash
# 1. Build & Push
docker build -t registry.richardx.dev/amael-agentic-backend:<version> .
docker push registry.richardx.dev/amael-agentic-backend:<version>

# 2. Actualizar versión en el manifest
# k8s/agents/05-backend-deployment.yaml

# 3. Aplicar
kubectl set image deployment/amael-agentic-deployment \
  amael-agentic-backend=registry.richardx.dev/amael-agentic-backend:<version> -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia
```

---

## Documentación

- [`CLAUDE.md`](./CLAUDE.md) — guía para Claude Code (arquitectura, gotchas, workflows)
- [`TECHNICAL_DESIGN_DOCUMENT.md`](./TECHNICAL_DESIGN_DOCUMENT.md) — diseño técnico detallado
- [`runbooks/`](./runbooks/) — 15 runbooks de remediación indexados en Qdrant
- [`k8s/`](./k8s/) — manifiestos de Kubernetes (deployment, configmap, RBAC, ingress, PVCs)
