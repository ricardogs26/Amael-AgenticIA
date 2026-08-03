# Runbook: distinguir "NotReady" de "caído" antes de remediar

## Descripción
`DEPLOYMENT_DEGRADED` (réplicas disponibles < deseadas) tiene al menos cuatro
causas con remediaciones distintas — y opuestas. Reiniciar sin distinguirlas es
la fuente principal de bucles de remediación en este clúster.

## Árbol de decisión

### 1. ¿El rollout está en curso?
```bash
kubectl rollout status deploy/<nombre> -n <ns> --timeout=5s
kubectl get deploy <nombre> -n <ns> \
  -o jsonpath='{.metadata.generation} {.status.observedGeneration}{"\n"}'
```
`observedGeneration < generation`, o condición `Progressing/ReplicaSetUpdated`
→ **está arrancando, no está enfermo**. Acción: `NO_ACTION`.

Un deployment que acaba de ser aplicado o reiniciado baja sus réplicas
disponibles durante segundos o minutos. Es el estado normal de un despliegue.

### 2. ¿Los contenedores murieron, o solo no pasan el readiness?
```bash
kubectl get pods -n <ns> -l app=<x> -o json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    for c in p['status'].get('containerStatuses',[]):
        print(p['metadata']['name'],'ready',c['ready'],'restarts',c['restartCount'],
              c.get('lastState',{}).get('terminated',{}).get('reason'))
"
```
- `restartCount` sube y hay `lastState.terminated` → **crashea de verdad**.
  Ver `crash_loop.md` u `oom_killed.md`.
- `restartCount` estable, sin `lastState`, `ready=false` → **está vivo pero su
  readiness probe falla**. Casi siempre es una dependencia, no el servicio.

### 3. Si es readiness: ¿qué dependencia falta?
Los servicios de `amael-ia` dependen de Postgres, Redis, Qdrant y Ollama. Si
varios deployments caen **a la vez**, la causa es compartida — no reinicies cada
uno por separado.

```bash
kubectl get pods -n amael-ia -l 'app in (postgres,redis,qdrant,ollama)'
kubectl logs -n amael-ia deploy/<afectado> --tail=50 | grep -iE "refused|timeout|unhealthy"
```
Señal típica: `ConnectTimeoutError` o `Max retries exceeded` hacia otro servicio.

Si la dependencia es Ollama → ver `ollama_gpu_restart_storm.md`.
Acción: **arreglar la dependencia**, no reiniciar a los dependientes.

### 4. ¿El pod pertenece a un Job?
Ver `job_pod_failed.md`. Nunca `ROLLOUT_RESTART`.

## Regla principal
Reiniciar solo cuando el contenedor **murió** y su dependencia está sana. En
cualquier otro caso, esperar o arreglar la causa compartida.

## Guardrails implementados
- `SRE_DEGRADED_MIN_CYCLES` (3): la degradación debe sobrevivir 3 ciclos
  consecutivos antes de alertar; los rollouts normales no llegan a tanto.
- `_is_rollout_in_progress()`: descarta deployments que están convergiendo.
- Ventana de verificación: no se reinicia algo reiniciado hace menos de
  `SRE_VERIFICATION_DELAY` (600 s).
- `_deployment_exists()`: no parchear un Deployment que no existe.

## Anti-patrón registrado
01-ago-2026: `trader-service` tardaba más en arrancar que el intervalo del loop.
Cada ciclo lo veía degradado y lo reiniciaba, abortando el arranque anterior:
14 ReplicaSets en 3 días. El deployment nunca llegó a estar listo hasta que se
activó una ventana de mantenimiento.
