# Amael-AgenticIA

Plataforma multi-agente modular basada en LangGraph y Ollama para automatización inteligente en entornos empresariales.

## Arquitectura

```
planner → grouper → batch_executor (loop) → supervisor
    ↑                                            │
    └──────── REPLAN (max 1 retry) ─────────────┘
```

## Agentes disponibles

| Agente | Estado | Descripción |
|--------|--------|-------------|
| `planner` | ✅ Producción | Descompone requests en planes ejecutables |
| `executor` | ✅ Producción | Ejecuta pasos en paralelo con ThreadPoolExecutor |
| `supervisor` | ✅ Producción | Evalúa calidad (0-10) y decide ACCEPT/REPLAN |
| `researcher` | ✅ Producción | RAG sobre documentos del usuario + búsqueda web |
| `productivity` | ✅ Producción | Google Calendar / Gmail via OAuth + Vault |
| `sre` | ✅ Producción | Loop autónomo 60s: observa → detecta → actúa |
| `coder` | 🔲 Roadmap Phase 7 | Generación y refactor de código |
| `devops` | 🔲 Roadmap Phase 7 | CI/CD, pipelines, infraestructura |
| `qa` | 🔲 Roadmap Phase 8 | Validación de resultados y pruebas |
| `memory_agent` | 🔲 Roadmap Phase 8 | Gestión de contexto y memoria episódica |

## Skills disponibles

| Skill | Estado |
|-------|--------|
| `kubernetes` | ✅ Producción |
| `rag` | ✅ Producción |
| `llm` | ✅ Producción |
| `vault` | ✅ Producción |
| `web` | ✅ Producción |
| `git` | 🔲 Roadmap |
| `filesystem` | 🔲 Roadmap |
| `api_call` | 🔲 Roadmap |

## Stack

- **Orquestación**: LangGraph StateGraph
- **LLM**: Ollama — qwen2.5:14b (chat), nomic-embed-text (embeddings)
- **API**: FastAPI + SSE streaming
- **Storage**: PostgreSQL + Redis + Qdrant + MinIO
- **Infraestructura**: Kubernetes (MicroK8s), GPU RTX 5070
- **Observabilidad**: Prometheus + Grafana (8 dashboards) + OpenTelemetry + Tempo

## Arranque

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Despliegue (Kubernetes)

```bash
docker build -t registry.richardx.dev/amael-agentic-backend:<version> .
docker push registry.richardx.dev/amael-agentic-backend:<version>
# Actualizar imagen en k8s/agents/05-backend-deployment.yaml
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
```

## Documentación

- [Technical Design Document](./TECHNICAL_DESIGN_DOCUMENT.md) — arquitectura detallada, decisiones de diseño, flujos end-to-end
