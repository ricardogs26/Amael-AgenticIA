# Runbook: Pod en estado Pending (FailedScheduling)

## Descripción
Un pod en Pending no ha sido asignado a ningún nodo. Esto ocurre cuando el scheduler no encuentra un nodo que satisfaga los requirements del pod.

## Causas en Amael-IA

### 1. GPU no disponible (más común — Ollama)
Ollama solicita `nvidia.com/gpu: 1`. Solo hay 1 GPU (RTX 5070) y ya está asignada.
- Síntoma en events: `Insufficient nvidia.com/gpu`
- IMPORTANTE: Para reiniciar Ollama NO usar rollout restart (crea nuevo pod Pending).
  Usar: `kubectl delete pod -l app=ollama -n amael-ia`
- Remediación: verificar si la GPU está asignada a otro pod y liberarla.

### 2. Recursos insuficientes (CPU/RAM)
El nodo no tiene suficiente CPU o RAM para satisfacer los requests del pod.
- Síntoma en events: `Insufficient cpu`, `Insufficient memory`
- Diagnóstico:
  ```bash
  kubectl describe node lab-home | grep -A5 "Allocated resources"
  kubectl top pods -n amael-ia
  ```
- Remediación: reducir requests de otro pod o aumentar capacidad del nodo.

### 3. PersistentVolume no disponible
El pod necesita un PVC que no existe o está bound a otro pod.
- Síntoma en events: `persistentvolumeclaim "xxx" not found`, `volume node affinity conflict`
- Remediación: verificar PVCs con `kubectl get pvc -n amael-ia`.

### 4. Node selector / tolerations no satisfechos
El pod tiene nodeSelector o tolerations que ningún nodo satisface.
- Síntoma en events: `node(s) didn't match node selector`

## Diagnóstico rápido
```bash
kubectl describe pod <pod> -n amael-ia | grep -A20 "Events:"
kubectl get events -n amael-ia --sort-by='.lastTimestamp' | tail -10
```

## Acción recomendada
Pod Pending por GPU → intervención manual (no rollout restart).
Pod Pending por recursos → notificar humano para evaluar scaling.
Pod Pending por PVC → verificar y corregir PVC manualmente.
