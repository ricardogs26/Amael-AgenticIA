# Runbook: LOADBALANCER_NO_IP

## Síntomas
- Service de tipo LoadBalancer muestra `<pending>` en EXTERNAL-IP
- Tráfico externo no llega al servicio
- NGINX ingress inaccesible desde fuera del clúster

## Causa raíz común
1. **MetalLB IPAddressPool mal configurado** — el rango de IPs no coincide con la subred del nodo
2. **MetalLB no está corriendo** — pods de MetalLB con errores
3. **Subred del nodo cambió** — IP del nodo cambió (ej. cambio de router/AP) y el pool de MetalLB apunta a subred anterior
4. **Pool agotado** — todas las IPs del pool ya están asignadas

## Diagnóstico
```bash
# Ver IPs asignadas por MetalLB
kubectl get ipaddresspool -n metallb-system
kubectl get l2advertisement -n metallb-system

# Ver IP del nodo
kubectl get node -o wide

# Ver estado de MetalLB
kubectl get pods -n metallb-system
kubectl logs -n metallb-system deploy/controller
```

## Remediación
```bash
# 1. Actualizar IPAddressPool con subred correcta del nodo
kubectl edit ipaddresspool -n metallb-system default-pool

# 2. Reiniciar controlador MetalLB para forzar reasignación
kubectl rollout restart deployment/controller -n metallb-system

# 3. Verificar asignación (puede tardar 30s)
kubectl get svc -n ingress --watch
```

## Acción del SRE Agent
NOTIFY_HUMAN — requiere conocer la subred correcta del nodo y modificar configuración de MetalLB.
