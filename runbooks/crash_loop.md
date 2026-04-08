# Runbook: CrashLoopBackOff

## Descripción
Un pod en CrashLoopBackOff indica que el contenedor arranca, falla, y Kubernetes lo reinicia repetidamente con backoff exponencial (10s, 20s, 40s, ..., 5min).

## Causas más comunes

### 1. Dependencia caída (causa #1 en Amael-IA)
El servicio intenta conectar a PostgreSQL, Redis, Qdrant u Ollama en el arranque y falla.
- Síntoma en logs: `connection refused`, `dial tcp`, `ECONNREFUSED`
- Acción: verificar que postgres-service, redis-service, qdrant-service estén Running.
- Remediación: rollout restart del servicio afectado DESPUÉS de que la dependencia esté healthy.

### 2. Variable de entorno faltante o incorrecta
El proceso falla al arrancar porque no encuentra una env var obligatoria.
- Síntoma en logs: `ValueError: missing required env`, `KeyError`, `raise ValueError`
- Acción: revisar el ConfigMap y los Secrets del deployment con `kubectl describe pod`.
- Remediación: corregir el ConfigMap/Secret y hacer rollout restart.

### 3. Error de código (bug en arranque)
El proceso lanza una excepción durante la inicialización.
- Síntoma en logs: `Traceback (most recent call last)`, `ImportError`, `ModuleNotFoundError`
- Acción: revisar los logs completos con `kubectl logs pod --previous`.
- Remediación: corregir el código, hacer build y deploy de nueva imagen.

### 4. OOMKilled en arranque
El pod es terminado por exceder el memory limit antes de estar Ready.
- Síntoma: `last_state.terminated.reason = OOMKilled`
- Acción: ver el runbook oom_killed.md
- Remediación: aumentar memory limit o reducir uso de memoria en arranque.

### 5. Liveness probe muy agresiva
El pod arranca lento y la liveness probe lo mata antes de estar listo.
- Síntoma en events: `Liveness probe failed`, `Back-off restarting failed container`
- Acción: revisar `initialDelaySeconds` en el deployment.
- Remediación: aumentar `initialDelaySeconds` o `failureThreshold`.

## Pasos de diagnóstico
```
1. kubectl logs <pod> --previous -n amael-ia     # logs del container anterior
2. kubectl describe pod <pod> -n amael-ia         # events y last state
3. kubectl get pods -n amael-ia                   # ver si dependencias están Running
4. kubectl logs deployment/postgres -n amael-ia   # verificar dependencias
```

## Remediación automática
- Si causa raíz es dependencia caída: esperar que dependencia sane, luego rollout restart.
- Si causa es código: NO hacer rollout restart (no ayuda). Escalar a humano.
- Máximo 3 reinicios automáticos en 15 minutos. Tras eso, escalar a humano.

## Servicios afectados en Amael-IA (frecuencia histórica)
- `backend-ia-deployment`: frecuente, típicamente por PostgreSQL o Redis no ready
- `productivity-service-deployment`: Vault sealed o sin credenciales OAuth
- `k8s-agent-deployment`: raro, típicamente por Ollama no disponible
