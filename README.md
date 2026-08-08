# Amael-AgenticIA

Plataforma multi-agente modular basada en LangGraph, FastAPI y Ollama para automatización inteligente. Corre como `amael-agentic-backend` en Kubernetes (MicroK8s, single-node, GPU RTX 5070), acompañada de los servicios standalone `raphael-service` (SRE) y `camael-service` (GitOps); el trader vive en su propio repo (`trader-service`).

**Versión actual:** `1.15.4` — desplegada por el pipeline de CI (push a `main` = deploy).

---

## Arquitectura

```
POST /api/chat  (frontend Next.js · WhatsApp bridge — ambos canales convergen aquí)
  → JWT auth → rate limit (Redis) → validate_prompt()
  → perfil del usuario (hechos destilados, inyectados SIEMPRE)
  → memoria episódica (recuperada por similitud)
  → AgentRouter.route()  (keywords → LLM fallback)
  → AgentDispatcher.dispatch()
       ├─ Ruta rápida (charla) ────────── qwen3:14b sin thinking, ~0.6 s
       ├─ Directo por intent ──────────── Raphael · Haniel · Sandalphon · Raziel ·
       │                                  Gabriel · Uriel · Jophiel · Camael* ·
       │                                  Phanuel (QA) · Cassiel · Zaphkiel
       └─ Pipeline LangGraph (general/k8s/monitoring):
            Sariel → Grouper → Batch Executor (loop) → Remiel
                ↑                                         │
                └────────── REPLAN (max 1 retry) ─────────┘

  * Camael corre remoto en camael-service:8003 (CAMAEL_MODE=remote)
```

La topología ya no se documenta a mano: se **deriva del código** y se sirve en
`GET /api/graph` — visible como grafo interactivo (estilo Obsidian) en
`/admin/graph`, con el tráfico real medido (`amael_agent_edge_total`) y la capa
de conocimiento (runbooks por tipo + colecciones Qdrant).

### Tiers de LLM (A2, ago-2026)

| Servidor | Modelos | Rol |
|---|---|---|
| `ollama-service` (GPU) | `qwen3:14b` (ctx **8192**, KV `q8_0`, 100 % VRAM, anclado — atiende también la ruta rápida sin thinking) · `qwen2.5vl:3b` (visión) | interactivo — **solo modelos causales** |
| `ollama-cpu-service` | `qwen3:30b-a3b` (tier profundo) · `nomic-embed-text` (**embeddings de toda la plataforma**, `OLLAMA_EMBED_URL`) | trabajo nocturno + embeddings |

Regla de oro: **ningún cliente manda `num_ctx`** — el contexto efectivo es del
runner cargado, y un cliente divergente fuerza una recarga del modelo en cada
alternancia (la «ruleta del runner», 7-ago-2026).

---

## Agentes (14 registrados)

| Agente | Nombre | Rol |
|--------|--------|-----|
| `planner` | Sariel | Descompone requests en planes JSON (max 8 pasos) |
| `executor` | — | Ejecuta pasos: tools en paralelo, REASONING secuencial |
| `supervisor` | Remiel | Evalúa calidad 0–10, decide ACCEPT/REPLAN |
| `researcher` | Sandalphon | RAG por usuario (Qdrant) + DuckDuckGo |
| `productivity` | Haniel | Google Calendar / Gmail vía OAuth + Vault |
| `sre` | Raphael | Loop autónomo 60 s + skills procedurales aprobadas (standalone :8002) |
| `cto` | Raziel | Estrategia técnica |
| `dev` | Gabriel | Código, commits y PRs (GitHub) |
| `arch` | Uriel | Arquitectura y ADRs |
| `coder` | Jophiel | Generación/análisis de código en memoria |
| `devops` | Camael | GitOps: PR + RFC ServiceNow (standalone :8003) |
| `qa` | Phanuel | Ejecución de tests y reporte CI |
| `memory` | Zaphkiel | Memoria episódica + búsqueda de historial (`pg_trgm`, mensajes crudos) |
| `reminder` | Cassiel | **Scheduler conversacional** — «recuérdame X cada lunes» → `user_jobs` |

## El bucle de aprendizaje (plan Hermes, ago-2026)

```
conversación → Zaphkiel guarda episodios (cada turno)
      ↓ 03:30 · consolidador (tier profundo)
hechos estables («prefiere respuestas cortas») → inyectados COMPLETOS al prompt
```
```
incidente → runbook (N1) → consolidado con fusión (N3, 03:00)
      ↓ máx 2/noche
propuesta de SKILL.md → aprobación humana por WhatsApp (/sre skill approve)
      ↓
entra al diagnóstico de la siguiente anomalía (catálogo breve + cuerpo bajo demanda)
```

El gate de las skills es **estructural**: `skill_manage` solo puede proponer;
la activación existe únicamente en el comando humano.

---

## SRE Agent — Raphael

Loop **Observe → Detect → Diagnose → Decide → Act → Report** cada 60 s, con
21 tipos de anomalía (cluster, Prometheus, tendencias, SLO, certificados, VRAM
del LLM), verificación post-acción a T+5 min, auto-rollback, postmortems LLM y
handoff GitOps a Camael. Watchdog externo independiente por si Raphael cae.
Detalle completo en [`CLAUDE.md`](./CLAUDE.md).

---

## Capacidades de plataforma

- **Scheduler conversacional**: `user_jobs` en Postgres, tick 60 s, claim con
  `FOR UPDATE SKIP LOCKED` (HPA corre 2 réplicas), entrega por WhatsApp.
- **Memoria destilada**: episodios → hechos (03:30, tier profundo); bloque de
  perfil con tope duro en código (900 chars), caché Redis invalidado por
  consolidación y GDPR-wipe.
- **Búsqueda de historial**: `pg_trgm` sobre `messages.content` — citas crudas
  con fecha y autor, ~0.1 s, 0 tokens.
- **DM pairing**: alta de números WhatsApp con códigos `AMAEL-XXXXXXXX` (1 h,
  canje atómico) — sin tocar ConfigMaps.
- **Ledger de entrega** (bridge 1.7.0): salientes persistidos en el PVC antes
  del send, reentrega al arrancar.
- **Multimedia**: transcripción (faster-whisper), visión (`qwen2.5vl:3b`), TTS
  (CosyVoice3 con voz clonada / Piper).

---

## Stack

| Capa | Tecnología |
|------|-----------|
| Orquestación | LangGraph StateGraph + AgentRegistry/SkillRegistry/ToolRegistry |
| LLM | Ollama — `qwen3:14b` (GPU, ctx 8192 + KV q8_0) · `qwen3:30b-a3b` (CPU, profundo) · `nomic-embed-text` (CPU) |
| API | FastAPI |
| Storage | PostgreSQL · Redis · Qdrant · MinIO |
| Infra | MicroK8s · RTX 5070 · MetalLB · NGINX Ingress · cert-manager · Vault |
| Observabilidad | Prometheus · Grafana (12 dashboards) · OpenTelemetry · Tempo |
| CI/CD | GitHub Actions (self-hosted): Gitleaks · pip-audit · Checkov · Bandit · pytest/ruff · build → deploy → hook a Raphael |

---

## Despliegue

**El backend se despliega por CI**: push a `main` con la versión actualizada en
`k8s/agents/05-backend-deployment.yaml` → el pipeline construye, pushea el tag,
aplica el manifest y notifica a Raphael (monitoreo intensificado 10 min).

```bash
# 1. Subir la versión en el manifest (single source of truth)
#    k8s/agents/05-backend-deployment.yaml
# 2. Commit + push a main → CI hace build, push, deploy y verificación
git push origin main
gh run watch   # opcional
```

Raphael/Camael se construyen del mismo repo con build args
(`APP_MODULE`/`APP_PORT`, ver CLAUDE.md § Build) y se aplican con `kubectl` —
nunca `kubectl set image` sin actualizar el manifest primero.

---

## Documentación

- [`CLAUDE.md`](./CLAUDE.md) — arquitectura detallada, gotchas, workflows
- [`docs/ANALISIS-HERMES-VS-AMAEL.md`](./docs/ANALISIS-HERMES-VS-AMAEL.md) — el plan del bucle de aprendizaje
- [`docs/ANALISIS-INFRA-LLM.md`](./docs/ANALISIS-INFRA-LLM.md) — presupuesto de VRAM y tuning de Ollama
- [`runbooks/`](./runbooks/) — 18 runbooks estáticos + consolidados autogenerados en Qdrant
- [`k8s/`](./k8s/) — manifiestos (agents, infrastructure, ingress, config)
