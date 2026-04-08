# Runbook: DiskPressure en Nodo

## Descripción
El nodo reporta DiskPressure=True cuando el espacio disponible en disco cae por debajo del umbral configurado (por defecto 10% o 1.5Gi libres). Kubernetes puede comenzar a evict pods para liberar espacio.

## Síntomas
- `kubectl describe node lab-home` muestra `DiskPressure: True`
- Los pods pueden ser evicted con reason `Evicted`
- Nuevas imágenes no pueden ser descargadas (ImagePullBackOff)

## Principales consumidores de disco en Amael-IA

### 1. Imágenes de Docker acumuladas
Las imágenes antiguas no se limpian automáticamente.
- Diagnóstico: `sudo du -sh /var/lib/containerd/`
- Remediación: `sudo ctr images prune --all` o `sudo crictl rmi --prune`

### 2. Logs de pods acumulados
Los logs de pods con muchos reinicios pueden crecer.
- Path: `/var/log/pods/`
- Remediación: `sudo find /var/log/pods -name "*.log" -mtime +3 -delete`

### 3. Datos de Ollama (modelos LLM)
Los modelos de Ollama son grandes (qwen2.5:14b ≈ 9GB).
- Path: `/var/snap/microk8s/common/default-storage/` o PVC montado
- NO borrar modelos en uso.

### 4. Datos de PostgreSQL / Qdrant / MinIO
Los PVCs de estas bases de datos pueden crecer.
- Diagnóstico: `kubectl get pvc -n amael-ia`

### 5. Snapshots de MicroK8s
- Path: `/var/snap/microk8s/`
- Remediación: `sudo snap remove microk8s --revision=<old>` para limpiar snapshots antiguos.

## Script de limpieza de emergencia
```bash
# Limpiar imágenes no usadas
sudo crictl rmi --prune

# Limpiar logs de pods antiguos (>3 días)
sudo find /var/log/pods -name "*.log" -mtime +3 -delete

# Ver qué está usando el disco
sudo df -h
sudo du -sh /var/snap/microk8s/common/* | sort -rh | head -10
sudo du -sh /var/log/pods/* | sort -rh | head -10
```

## Acción del agente SRE
DiskPressure siempre requiere notificación al humano.
El agente no puede ejecutar comandos en el nodo (no tiene acceso SSH).
Umbrales sugeridos para alertas tempranas: >80% uso de disco.
