# Runbook: ImagePullBackOff / ErrImagePull

## Descripción
El nodo no puede descargar la imagen del contenedor. No se puede remediar automáticamente — requiere intervención humana para corregir el tag de imagen o las credenciales del registry.

## Causas

### 1. Tag de imagen incorrecto
El deployment especifica una versión de imagen que no existe en el registry.
- Síntoma en events: `Failed to pull image "registry.richardx.dev/...": not found`
- Acción: verificar qué tags existen en `registry.richardx.dev`.
  ```bash
  docker images registry.richardx.dev/<service>
  kubectl get deployment <name> -o jsonpath='{.spec.template.spec.containers[].image}'
  ```
- Remediación: actualizar el deployment con el tag correcto.

### 2. Registry privado no accesible
El clúster no puede alcanzar `registry.richardx.dev`.
- Síntoma en events: `connection timed out`, `no such host`
- Acción: verificar conectividad del nodo al registry.
  ```bash
  kubectl run test --image=busybox --restart=Never -- curl -v https://registry.richardx.dev/v2/
  ```

### 3. Credenciales de registry expiradas o incorrectas
- Acción: verificar el imagePullSecret del namespace.
  ```bash
  kubectl get secrets -n amael-ia | grep registry
  kubectl describe secret <registry-secret> -n amael-ia
  ```

### 4. Disco lleno en el nodo (no puede extraer la imagen)
- Acción: ver runbook disk_pressure.md

## Acción recomendada
**Esta anomalía siempre requiere intervención manual.** El agente SRE NO puede corregir automáticamente un tag de imagen incorrecto.

Pasos:
1. Verificar el tag de imagen correcto en el registry.
2. Editar el deployment YAML con el tag correcto.
3. `kubectl apply -f k8s/<deployment>.yaml -n amael-ia`
