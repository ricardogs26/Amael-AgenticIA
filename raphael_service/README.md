# raphael-service

Microservicio SRE autónomo. Empaqueta `agents.sre` del monorepo
`Amael-AgenticIA` como un FastAPI independiente, desplegado en un pod
separado del backend principal. Reemplaza al legacy **k8s-agent 5.2.0**.

## Puerto

**8002** (drop-in replacement del k8s-agent legacy — mismo puerto para
no romper `K8S_AGENT_URL` ya configurado en el resto del cluster).

## Build

```bash
# Desde la raíz del repo Amael-AgenticIA (no desde raphael_service/):
docker build -t registry.richardx.dev/raphael-service:1.0.0 \
             -f raphael_service/Dockerfile .
docker push registry.richardx.dev/raphael-service:1.0.0
```

## Rutas absorbidas del legacy k8s-agent

| Ruta                         | Origen           | Auth                |
|------------------------------|------------------|---------------------|
| `POST /api/k8s-agent`        | legacy 5.2.0     | `INTERNAL_API_SECRET` |
| `POST /api/sre/deploy-hook`  | nuevo (CI hook)  | `INTERNAL_API_SECRET` |
| `GET /api/sre/loop/status`   | monorepo router  | público (interno)   |
| `GET /api/sre/incidents`     | monorepo router  | `INTERNAL_API_SECRET` |
| `GET /api/sre/postmortems`   | monorepo router  | `INTERNAL_API_SECRET` |
| `GET /api/sre/learning/stats`| monorepo router  | `INTERNAL_API_SECRET` |
| `GET /api/sre/slo/status`    | monorepo router  | `INTERNAL_API_SECRET` |
| `*/api/sre/maintenance`      | monorepo router  | operator             |
| `POST /api/sre/command`      | monorepo router  | `INTERNAL_API_SECRET` |
| `GET /health`, `GET /ready`  | observability    | público             |
| `GET /metrics`               | prometheus       | público             |

## Variables de entorno críticas

```
POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
REDIS_HOST, REDIS_PORT
QDRANT_URL=http://qdrant-service:6333
OLLAMA_BASE_URL=http://ollama-service:11434
PROMETHEUS_URL=http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090
INTERNAL_API_SECRET=<shared con backend/whatsapp-bridge>
JWT_SECRET_KEY=<shared con backend>
SRE_LOOP_ENABLED=true
SRE_LOOP_INTERVAL=60
APP_VERSION=1.0.0
```

## Dev local

```bash
cd Amael-AgenticIA
PYTHONPATH=. python raphael_service/main.py
```

El código es totalmente importable sin ejecutar (apto para pytest).
