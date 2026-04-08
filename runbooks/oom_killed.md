# Runbook: OOMKilled

## Descripción
OOMKilled (Out Of Memory Killed) ocurre cuando un contenedor supera su `resources.limits.memory`. El kernel de Linux mata el proceso con SIGKILL y Kubernetes registra el motivo como OOMKilled.

## Síntomas
- `kubectl describe pod` muestra: `Last State: Terminated (OOMKilled)`
- `exit_code: 137` (128 + SIGKILL)
- El pod se reinicia y eventualmente entra en CrashLoopBackOff si la causa persiste.

## Causas en Amael-IA

### 1. Carga inusual de datos (productivity-service)
La integración con Gmail/Calendar procesa un volumen grande de emails o eventos.
- Síntoma en logs: `Processing X emails`, `Memory allocation failed`
- Memory limit actual: 512Mi
- Remediación: aumentar limit a 1Gi y añadir paginación en la API de Google.

### 2. Modelo LLM en memoria (backend-ia, k8s-agent)
Carga de contexto muy grande satura la memoria del proceso Python.
- Síntoma: log de la conversación era extremadamente larga
- Memory limit backend-ia: ~1Gi (verificar deployment)
- Remediación: rollout restart + revisar MAX_CONTEXT_CHARS.

### 3. Memory leak acumulativo
El pod lleva muchas horas corriendo y la memoria creció gradualmente.
- Diagnóstico: ver métricas `container_memory_working_set_bytes` en Prometheus.
- Remediación: rollout restart (fix temporal) + investigar el leak.

### 4. Qdrant o Redis procesando datos masivos
- Remediación: rollout restart del pod afectado.

## Pasos de diagnóstico
```
1. kubectl describe pod <pod> -n amael-ia         # confirmar OOMKilled
2. kubectl logs <pod> --previous -n amael-ia      # qué estaba haciendo antes de morir
3. Prometheus query: max_over_time(container_memory_working_set_bytes{pod=~"<pod>.*"}[1h])
4. Comparar con limits: kubectl get pod <pod> -o jsonpath='{.spec.containers[].resources}'
```

## Remediación automática
- Rollout restart inmediato (libera memoria, pod arranca fresco).
- El fix permanente requiere: aumentar limit O reducir uso de memoria en código.
- Si el pod vuelve a OOMKilled en <15min, escalar a humano (aumentar limit manualmente).

## PromQL útiles
```promql
# Uso de memoria actual vs limit
sum(container_memory_working_set_bytes{namespace="amael-ia"}) by (pod)

# Tendencia de memoria en la última hora
rate(container_memory_working_set_bytes{namespace="amael-ia"}[5m])
```
