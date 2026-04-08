# Runbook: High Restart Rate

## Descripción
Un pod acumula un alto número de reinicios (≥5) pero actualmente está en estado Running. Indica inestabilidad crónica: el pod sigue funcionando pero falla periódicamente.

## Causas

### 1. Liveness probe fallando intermitentemente
La liveness probe falla cuando el servicio está bajo carga y el pod se reinicia.
- Síntoma en events: `Liveness probe failed`
- Diagnóstico: revisar `livenessProbe.timeoutSeconds` vs tiempo de respuesta real.
- Remediación: aumentar `timeoutSeconds` o `failureThreshold`.

### 2. Dependencia intermitente
El servicio pierde conexión a PostgreSQL/Redis/Qdrant y no la recupera.
- Síntoma en logs: mensajes de reconexión, pool exhausted.
- Remediación: rollout restart + verificar connection pooling en el código.

### 3. Memory leak lento
El servicio crece en memoria gradualmente y es matado por OOM cada X horas.
- Diagnóstico Prometheus: `container_memory_working_set_bytes` aumenta linealmente.
- Remediación: rollout restart (temporal) + investigar leak.

### 4. Timeout de LLM en liveness probe
Si el health check del agente llama al LLM y este está lento, la probe falla.
- Síntoma: reinicios correlacionan con carga alta de Ollama.

## Diagnóstico
```bash
kubectl describe pod <pod> -n amael-ia | grep -E "Restart Count|Last State|Liveness"
kubectl logs <pod> -n amael-ia --previous  # logs del reinicio anterior
```

## PromQL útil
```promql
# Tasa de reinicios en los últimos 15 minutos
increase(kube_pod_container_status_restarts_total{namespace="amael-ia"}[15m])
```

## Remediación automática
- rollout restart del deployment (limpia estado acumulado).
- Si vuelve a reiniciarse en <15min, escalar a humano para investigar causa raíz.
