# Runbook: VAULT_SEALED

## Síntomas
- Vault reporta status 503 en `/v1/sys/health`
- Skills que dependen de Vault (OAuth, tokens Google) fallan con errores de autenticación
- `skill.vault` aparece rojo en el dashboard de salud

## Causa raíz
Vault utiliza sellado de Shamir (3 de 5 claves). Cada vez que el pod `vault-0` se reinicia,
Vault arranca en estado sellado y requiere unsealing manual. No puede hacer esto automáticamente
porque las claves de unseal no se almacenan en el clúster por razones de seguridad.

## Diagnóstico
```bash
# Verificar estado de Vault
kubectl exec -n vault vault-0 -- vault status

# Verificar si el pod está corriendo
kubectl get pod -n vault vault-0
```

## Remediación — REQUIERE INTERVENCIÓN HUMANA
Las claves de unseal están en `vault.root` (NUNCA en git).

```bash
kubectl port-forward -n vault svc/vault 8200:8200 &
export VAULT_ADDR="http://localhost:8200"
vault operator unseal <KEY_1>
vault operator unseal <KEY_2>
vault operator unseal <KEY_3>
```

## Prevención
- Configurar auto-unseal con un KMS externo (AWS KMS, GCP Cloud KMS)
- Agregar monitoreo de liveness que detecte el sellado antes de que afecte servicios

## Acción del SRE Agent
NOTIFY_HUMAN — no se puede unsealar automáticamente (requiere claves Shamir secretas).
