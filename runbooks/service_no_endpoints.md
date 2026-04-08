# Runbook: SERVICE_NO_ENDPOINTS

## Síntomas
- Service existe pero no tiene endpoints saludables
- Requests al Service retornan connection refused o timeout
- Posiblemente acompañado de pods NotReady

## Causa raíz común
1. **Pods NotReady** — readiness probe fallando, pods no pasan a Ready
2. **Label mismatch** — el selector del Service no coincide con los labels de los pods
3. **Todos los pods en CrashLoop** — no hay pods en estado Running+Ready
4. **Deployment sin réplicas** — scaled a 0 o no hay pods creados

## Diagnóstico
```bash
# Ver endpoints del service
kubectl get endpoints <service-name> -n <namespace>
kubectl describe endpoints <service-name> -n <namespace>

# Ver pods y su estado
kubectl get pods -n <namespace> -l <selector-label>=<valor>

# Verificar selector del Service vs labels de pods
kubectl get svc <service-name> -n <namespace> -o jsonpath='{.spec.selector}'
kubectl get pods -n <namespace> --show-labels
```

## Remediación
```bash
# Si pods están NotReady por readiness probe:
kubectl describe pod <nombre> -n <namespace>  # ver causa del probe failure

# Si label mismatch, corregir el Service selector o los pod labels
kubectl edit svc <service-name> -n <namespace>

# Si pods en CrashLoop, ver logs:
kubectl logs <pod-name> -n <namespace> --previous
```

## Acción del SRE Agent
NOTIFY_HUMAN — requiere diagnóstico de por qué los pods no están Ready.
Si los pods están en CrashLoop, el SRE agent generará adicionalmente una anomalía CRASH_LOOP
con acción ROLLOUT_RESTART.
