# Infraestructura — Servicios Base

Manifiestos Kubernetes para los servicios de infraestructura requeridos por Amael-AgenticIA.

## Servicios

| Archivo | Servicio | Puerto | Storage |
|---------|---------|--------|---------|
| `00-namespace.yaml` | Namespace `amael-ia` | — | — |
| `01-postgres.yaml` | PostgreSQL 16 | 5432 | 10Gi PVC |
| `02-redis.yaml` | Redis 7 | 6379 | — (en memoria) |
| `03-qdrant.yaml` | Qdrant v1.9.4 | 6333 (HTTP) / 6334 (gRPC) | 10Gi PVC |
| `04-minio.yaml` | MinIO | 9000 (API) / 9001 (Console) | 20Gi PVC |
| `05-ollama.yaml` | Ollama 0.4.6 | 11434 | 50Gi PVC |
| `06-allowed-users-configmap.yaml` | Whitelist usuarios | — | — |

**Total storage requerido**: ~90Gi

## Orden de despliegue (primer arranque)

```bash
# 0. Namespace primero
kubectl apply -f k8s/infrastructure/00-namespace.yaml

# 1. Secrets (ANTES que cualquier servicio que los requiera)
#    Editar k8s/config/02-secrets.yaml con los valores reales
kubectl apply -f k8s/config/02-secrets.yaml -n amael-ia

# 2. Whitelist de usuarios
kubectl apply -f k8s/infrastructure/06-allowed-users-configmap.yaml

# 3. Bases de datos (sin dependencias entre sí — se pueden aplicar en paralelo)
kubectl apply -f k8s/infrastructure/01-postgres.yaml
kubectl apply -f k8s/infrastructure/02-redis.yaml
kubectl apply -f k8s/infrastructure/03-qdrant.yaml
kubectl apply -f k8s/infrastructure/04-minio.yaml

# 4. Ollama (requiere GPU disponible)
kubectl apply -f k8s/infrastructure/05-ollama.yaml

# 5. Esperar que todo esté Ready
kubectl wait --for=condition=ready pod -l app=postgres -n amael-ia --timeout=120s
kubectl wait --for=condition=ready pod -l app=redis   -n amael-ia --timeout=60s
kubectl wait --for=condition=ready pod -l app=qdrant  -n amael-ia --timeout=120s
kubectl wait --for=condition=ready pod -l app=minio   -n amael-ia --timeout=120s
kubectl wait --for=condition=ready pod -l app=ollama  -n amael-ia --timeout=300s

# 6. Descargar modelos Ollama (solo en el primer arranque)
OLLAMA_POD=$(kubectl get pod -l app=ollama -n amael-ia -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n amael-ia $OLLAMA_POD -- ollama pull qwen2.5:14b
kubectl exec -n amael-ia $OLLAMA_POD -- ollama pull nomic-embed-text
kubectl exec -n amael-ia $OLLAMA_POD -- ollama pull qwen2.5-vl:7b   # opcional (visión)

# 7. ConfigMap y RBAC del backend
kubectl apply -f k8s/config/01-configmap.yaml
kubectl apply -f k8s/rbac/03-serviceaccount.yaml
kubectl apply -f k8s/rbac/04-sre-rbac.yaml

# 8. Backend
kubectl apply -f k8s/agents/05-backend-deployment.yaml
kubectl apply -f k8s/agents/07-day-planner-cronjob.yaml

# 9. Ingress
kubectl apply -f k8s/ingress/06-ingress.yaml
```

## Verificar estado

```bash
kubectl get pods -n amael-ia
kubectl get pvc -n amael-ia
kubectl get svc -n amael-ia
```

## Notas importantes

### PostgreSQL

- **DB**: `amael_db` | **User**: `amael_user` | **Password**: Secret `amael-secrets → POSTGRES_PASSWORD`
- Las tablas se crean automáticamente al arrancar el backend (`_ensure_schema()` en `main.py`)
- Tablas: `conversations`, `messages`, `user_documents`, `sre_incidents`, `sre_postmortems`

### Qdrant

- Versión **pinada a v1.9.4** — `latest` puede traer breaking changes en la API
- En v1.7+ se eliminó `search()` → usar `query_points()` (ya corregido en el código)
- Las colecciones se crean automáticamente al primer uso

### MinIO

- Consola web disponible en `http://<node-ip>:30090` (NodePort)
- Credenciales: Secret `amael-secrets → MINIO_ACCESS_KEY` / `MINIO_ROOT_PASSWORD`
- Los buckets se crean automáticamente al subir el primer documento

### Ollama

- **NO usar `kubectl rollout restart`** — con la GPU única el pod nuevo queda Pending
- Restart correcto: `kubectl delete pod -l app=ollama -n amael-ia`
- `OLLAMA_KEEP_ALIVE=24h` mantiene los modelos cargados en VRAM entre requests

### Redis

- Sin persistencia (datos en memoria) — al reiniciar se pierden sesiones y rate-limit counters
- Impacto: usuarios necesitarán esperar 60s para que se resetee el rate limit tras reinicio
- Para producción crítica: agregar `appendonly yes` y montar un PVC

## Servicios de soporte (en Amael-IA/k8s/)

Los siguientes servicios también corren en el namespace `amael-ia` pero sus
manifiestos viven en el repositorio `Amael-IA`:

| Servicio | Manifesto origen | Puerto |
|---------|-----------------|--------|
| `k8s-agent` | `Amael-IA/k8s/19.-k8s-agent-deployment.yaml` | 8002 |
| `productivity-service` | `Amael-IA/k8s/20.-productivity-deployment.yaml` | 8001 |
| `whatsapp-bridge` | `Amael-IA/k8s/08.-whatsapp-deployment.yaml` | 3000 |
| `frontend-next` | `Amael-IA/k8s/27.-frontend-next-deployment.yaml` | 3000 |
