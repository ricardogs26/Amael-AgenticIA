# Runbook: DEPLOYMENT_DEGRADED

## Síntomas
- Deployment tiene réplicas disponibles < réplicas deseadas
- Pods en estado CrashLoopBackOff, Pending o Error
- Servicio puede estar degradado o inaccesible (si available=0)

## Causa raíz común
1. **Pod en CrashLoop** — el contenedor falla al arrancar (misconfiguration, error de código)
2. **OOM** — pod asesinado por límite de memoria
3. **ImagePullError** — imagen no disponible en el registry
4. **Recursos insuficientes** — nodo sin CPU/memoria para el nuevo pod
5. **PVC no disponible** — volume mount falla al arrancar

## Diagnóstico
```bash
# Ver estado del deployment
kubectl describe deployment <nombre> -n <namespace>

# Ver pods con problemas
kubectl get pods -n <namespace> | grep -v Running

# Ver eventos recientes
kubectl get events -n <namespace> --sort-by='.lastTimestamp' | tail -20
```

## Remediación
```bash
# Rollout restart (reinicia pods gradualmente)
kubectl rollout restart deployment/<nombre> -n <namespace>

# Si hay imagen incorrecta, actualizar primero:
kubectl set image deployment/<nombre> <container>=<imagen>:<tag> -n <namespace>

# Verificar rollout
kubectl rollout status deployment/<nombre> -n <namespace>
```

## Acción del SRE Agent
ROLLOUT_RESTART — si confianza >= threshold y no es deployment protegido.
El agente intentará restaurar las réplicas con un rollout restart.
Si el problema persiste después de 5 minutos, notificará al humano.
