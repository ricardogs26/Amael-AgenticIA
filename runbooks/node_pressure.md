# Runbook: NODE_PRESSURE

## Síntomas
- Nodo con condición DiskPressure, MemoryPressure o PIDPressure = True
- Pods siendo evicted del nodo
- Nuevos pods no pueden schedularse en el nodo afectado

## Tipos de presión

### DiskPressure (más crítico)
- Disco del nodo casi lleno
- Kubelet empieza a evictar pods para liberar espacio
- Si llega a 100%, el nodo puede volverse inoperable

### MemoryPressure
- Memoria del nodo casi agotada
- OOM killer puede matar procesos arbitrarios

### PIDPressure
- Demasiados procesos corriendo en el nodo
- Raro en single-node, indica posible fork bomb o leak

## Diagnóstico
```bash
# Ver uso de disco en el nodo
df -h /
du -sh /var/lib/containerd/  # imágenes de contenedores

# Ver uso de memoria
free -h
kubectl top node

# Ver pods siendo evicted
kubectl get events --all-namespaces | grep Evict
```

## Remediación DiskPressure
```bash
# Limpiar imágenes no usadas
docker system prune -a --volumes  # o crictl rmi

# Identificar qué usa más espacio
du -sh /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/ | sort -h | tail

# Si logs de pods son muy grandes:
kubectl logs --all-namespaces --tail=0  # ver tamaños en /var/log/pods/
```

## Acción del SRE Agent
NOTIFY_HUMAN — no puede limpiar disco ni memoria del nodo autónomamente.
La evicción de pods es manejada por Kubernetes automáticamente.
