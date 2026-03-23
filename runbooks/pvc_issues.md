# Runbook: PVC_PENDING / PVC_MOUNT_ERROR

## Síntomas PVC_PENDING
- PersistentVolumeClaim en estado Pending por más de 5 minutos
- Pods que dependen del PVC quedan en Pending esperando el volumen

## Síntomas PVC_MOUNT_ERROR
- Evento `FailedMount` en el pod que necesita el volumen
- Evento `FailedAttachVolume` — el volumen no se puede adjuntar al nodo
- Pod en estado ContainerCreating indefinidamente

## Causa raíz común
1. **StorageClass no disponible** — la StorageClass referenciada no existe
2. **Permisos incorrectos** — el directorio del PVC es owned por root, el contenedor corre como usuario no-root
3. **Volumen ya montado en otro pod** — PVC con accessMode ReadWriteOnce montado en otro nodo
4. **Directorio host no existe** — para hostPath/local storage
5. **Cuota de almacenamiento agotada**

## Diagnóstico
```bash
# Ver estado del PVC
kubectl describe pvc <nombre> -n <namespace>

# Ver eventos del pod
kubectl describe pod <nombre> -n <namespace> | grep -A 10 Events

# Ver StorageClasses disponibles
kubectl get storageclass
```

## Remediación permisos (causa más común en microk8s-hostpath)
```bash
# Agregar securityContext.fsGroup al Deployment (si fsGroup no funciona con hostpath):
# Agregar initContainer que haga chown:
kubectl patch deployment <nombre> -n <namespace> --type=json -p='[
  {"op":"add","path":"/spec/template/spec/initContainers/-","value":{
    "name":"fix-perms","image":"busybox:1.36",
    "command":["sh","-c","chown -R 1000:1000 /mount && chmod 755 /mount"],
    "volumeMounts":[{"name":"<vol-name>","mountPath":"/mount"}]
  }}
]'
```

## Acción del SRE Agent
NOTIFY_HUMAN — los problemas de PVC requieren entender el contexto de almacenamiento.
