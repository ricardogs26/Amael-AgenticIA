# Runbook: reinicio de Ollama y caída en cascada de servicios

## Descripción
`ollama-deployment` es el único consumidor de la GPU (`nvidia.com/gpu: 1`) del nodo
`lab-home`. Cuando su pod se recrea, arrastra consigo a los servicios que dependen
del LLM. El síntoma que llega al humano no es "Ollama se reinició" sino
"tres deployments sin réplicas disponibles".

## Firma del incidente
1. Un pod nuevo de `ollama-deployment` con `startTime` reciente.
2. Pico de `node_load5` (~4) **con CPU por pod baja** — es I/O, no cómputo: está
   leyendo el modelo (`qwen3:14b` pesa 9.3 GB) del disco a memoria/GPU.
3. En la misma ventana, `amael-agentic-deployment`, `raphael-service` y
   `whatsapp-bridge-deployment` quedan `0/N` disponibles.
4. Los pods afectados **no se reinician**: se quedan `Running` pero `NotReady`.
   `restartCount` no sube y `lastState.terminated` está vacío.

Ese último punto es el discriminador clave: si los pods no murieron, no es un
crash — es que sus readiness probes fallan porque su dependencia (Ollama) no
responde todavía.

## Verificación
```bash
kubectl get pods -n amael-ia -l app=ollama \
  -o custom-columns=NAME:.metadata.name,START:.status.startTime,RESTARTS:.status.containerStatuses[0].restartCount

# ¿Los afectados murieron o solo están NotReady?
kubectl get pods -n amael-ia -o json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    for c in p['status'].get('containerStatuses',[]):
        ls=c.get('lastState',{}).get('terminated')
        if ls: print(p['metadata']['name'], ls.get('reason'), ls.get('finishedAt'))
"
```

## Regla principal
**Esperar, no reiniciar.** Cargar el modelo tarda varios minutos. Reiniciar los
servicios dependientes durante ese periodo alarga la caída: vuelven a arrancar,
vuelven a no encontrar el LLM y vuelven a quedar NotReady.

Antes de actuar sobre cualquier deployment `NotReady`, comprobar si Ollama está
arrancando. Si lo está, la acción correcta es `NO_ACTION` y reevaluar en el
siguiente ciclo.

```bash
# ¿Ollama ya responde?
kubectl exec -n amael-ia deploy/ollama-deployment -- ollama list
```

## Causas de que Ollama se recree
- **Pods huérfanos reteniendo la GPU** tras reinicio del nodo: quedan en
  `ContainerStatusUnknown` o `UnexpectedAdmissionError`
  (`Allocate failed due to no healthy devices present`). Ver `pod_rejected.md`.
- Reinicio del nodo (`lab-home` tiene un historial de MCE del controlador de memoria).
- `rollout restart` manual — **no hacerlo**: con una sola GPU, RollingUpdate deja
  el pod nuevo en `Pending` porque el viejo aún retiene el dispositivo.

## Remediación
```bash
# Forma correcta de reiniciar Ollama (NUNCA rollout restart)
kubectl delete pod -l app=ollama -n amael-ia

# Limpiar huérfanos que retienen la GPU
kubectl delete pod -n amael-ia --field-selector=status.phase=Failed
```

Tras la limpieza, esperar a que el pod nuevo cargue el modelo antes de tocar nada más.

## Prevención
- Raphael limpia automáticamente los pods rechazados (`POD_REJECTED`) y los
  huérfanos (`POD_STATUS_UNKNOWN`) — eso libera la GPU sin intervención.
- `amael-watchdog` avisa por WhatsApp si algún deployment crítico se queda sin
  réplicas más de un ciclo.

## Historial
- 31-jul-2026: 5 pods huérfanos de Ollama retuvieron la GPU ~15 h sin LLM.
- 03-ago-2026: pod recreado a las 09:40 UTC; backend, raphael y whatsapp-bridge
  quedaron NotReady ~20 min. Se recuperaron solos sin intervención.
