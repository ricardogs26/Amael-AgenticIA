# Runbook: pod de Job / CronJob en Error

## Descripción
Un pod cuyo `ownerReferences[].kind == "Job"` terminó con exit code distinto de 0.
No es un pod de Deployment: **no existe ningún Deployment con su nombre** y por lo
tanto `ROLLOUT_RESTART` es siempre la acción equivocada.

## Cómo reconocerlo
- El nombre sigue el patrón `<cronjob>-<timestamp>-<hash>`, p. ej. `amael-watchdog-29762490-88lpk`.
- `kubectl get pod <pod> -o jsonpath='{.metadata.ownerReferences[0].kind}'` → `Job`.
- `kubectl get deploy <nombre-del-pod>` → `NotFound`.

## Regla principal
**No auto-remediar.** El controlador de Job ya reintenta según `backoffLimit`, y el
CronJob volverá a ejecutarse en su siguiente `schedule`. Raphael debe responder
`NO_ACTION` y, si el fallo es relevante, notificar.

Intentar `ROLLOUT_RESTART` sobre estos pods produce `404 Not Found` en cada ciclo
del loop, indefinidamente, porque la anomalía nunca se resuelve sola.

## Jobs que fallan a propósito
`amael-watchdog` (CronJob cada 10 min) **sale con exit 1 cuando detecta un
deployment sin réplicas disponibles**. Ese exit 1 *es* la alerta, por diseño:
deja el Job en `Failed` para que sea visible en `kubectl get jobs`.

Por lo tanto:
- Un `amael-watchdog-*` en `Error` **no es un incidente del watchdog**.
- Es un síntoma: *otro* deployment estaba caído en ese momento.
- La acción correcta es leer sus logs y atender el deployment que reportó.

```bash
kubectl logs -n amael-ia <pod-watchdog>
# → "`amael-ia/<deployment>` SIN RÉPLICAS DISPONIBLES (0/N)."
```

## Diagnóstico cuando el Job falla de verdad
1. Leer los logs del pod: `kubectl logs -n <ns> <pod>`.
2. Revisar si superó `activeDeadlineSeconds` (aparece como `DeadlineExceeded`).
3. Revisar `backoffLimit`: si se agotó, el CronJob no reintentará hasta el
   siguiente schedule.
4. Los CronJobs con `concurrencyPolicy: Forbid` se saltan ejecuciones si la
   anterior sigue activa — revisar si hay un Job colgado.

## Remediación manual (solo si el Job es realmente el problema)
```bash
# Relanzar un CronJob a mano
kubectl create job --from=cronjob/<cronjob> <nombre-manual> -n <ns>

# Limpiar Jobs fallidos acumulados
kubectl delete job -n <ns> --field-selector status.successful=0
```

## Prevención
- `failedJobsHistoryLimit` bajo (5) para que no se acumulen lápidas.
- Los pods de Job se excluyen de auto-remediación en `healer.decide_action()`
  vía `metadata["owner_kind"] == "Job"`.

## Anti-patrón registrado
Incidente 03-ago-2026: 16 patches `404` en 6 horas contra
`deployments/amael-watchdog-29762490-88lpk`, un pod de Job. El observer dejaba
`owner_name` vacío para pods de Job y el healer caía al nombre del pod.
