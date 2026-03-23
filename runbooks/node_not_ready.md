# Runbook: Node NotReady

## Descripción
El nodo `lab-home` (único nodo del clúster MicroK8s) reporta Ready=False. Esto significa que el kubelet no está reportando estado al API server. Todo el clúster está efectivamente down o degradado.

## Síntomas
- `kubectl get nodes` muestra `NotReady`
- Todos los pods en el nodo pueden estar en estado Unknown
- El API server puede seguir respondiendo si está en otro nodo (no aplica — single-node)

## Causas más comunes

### 1. Kubelet caído o no responde
- Diagnóstico (SSH al nodo): `systemctl status snap.microk8s.daemon-kubelet`
- Remediación: `sudo snap restart microk8s`

### 2. Nodo reiniciado (sistema operativo)
- El nodo se reinició y los servicios de MicroK8s no arrancaron solos.
- Remediación:
  ```bash
  sudo microk8s start
  sudo microk8s status
  ```

### 3. Disco lleno (kubelet no puede escribir logs)
- Ver runbook disk_pressure.md
- El kubelet requiere espacio para escribir logs y manifests.

### 4. Red del nodo caída
- El nodo perdió conectividad con el API server.
- Diagnóstico: hacer ping al nodo desde otra máquina.

### 5. OOM del sistema (no del container, del sistema operativo)
- El kernel mató el proceso kubelet por OOM del sistema.
- Remediación: SSH al nodo, reiniciar kubelet.
  ```bash
  sudo systemctl restart snap.microk8s.daemon-kubelet
  ```

## IMPORTANTE para Amael-IA (single-node cluster)
En un clúster single-node, Node NotReady es un incidente CRÍTICO de máxima prioridad.
- El agente SRE NO puede hacer nada remotamente (el nodo es inaccesible).
- Acción única: notificar al administrador de forma urgente y escalar.
- Requiere acceso físico o SSH al servidor `lab-home`.

## Remediación completa
```bash
# Conectar vía SSH al nodo
ssh richardx@lab-home

# Reiniciar MicroK8s
sudo snap restart microk8s
sudo microk8s status --wait-ready

# Después del reinicio, dessellar Vault
kubectl port-forward -n vault svc/vault 8200:8200 &
vault operator unseal <KEY_1>
vault operator unseal <KEY_2>
vault operator unseal <KEY_3>
```
