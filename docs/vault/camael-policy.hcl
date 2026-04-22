# ============================================================================
# Vault Policy: camael-service
# ----------------------------------------------------------------------------
# Destinada al ServiceAccount `camael-sa` (namespace `amael-ia`) que corre
# el agente Camael (DevOps/GitOps) separado del backend.
#
# Principio: least-privilege. Camael NO toca credenciales de Google OAuth
# (eso es exclusivo de Productivity). Necesita:
#   - Bitbucket (app password, username, workspace) para push de manifests y PRs.
#   - ServiceNow (instance, user, pass, client_id/secret) para abrir RFCs.
#   - Shared internal-secret para auth pod-to-pod (handoff desde Raphael).
#   - Ollama API key (reservado) para futuras llamadas autenticadas al adapter.
#
# KV v2 => los datos viven en `secret/data/...`, metadata en `secret/metadata/...`.
# Para políticas de solo lectura basta con `secret/data/...`.
# ============================================================================

# ----------------------------------------------------------------------------
# LECTURA — Credenciales Bitbucket
# Paths esperados bajo este prefijo:
#   secret/data/amael/bitbucket/app-password
#   secret/data/amael/bitbucket/username
#   secret/data/amael/bitbucket/workspace
# Hoy viven en el Secret K8s `bitbucket-credentials` (amael-ia). La migración
# a Vault mantiene el mismo dataset bajo un prefijo versionable y auditable.
# ----------------------------------------------------------------------------
path "secret/data/amael/bitbucket/*" {
  capabilities = ["read"]
}

# Listado de metadata bajo bitbucket/* — útil para discovery de sub-keys
# sin exponer el payload. Solo lista, no lee metadata sensible.
path "secret/metadata/amael/bitbucket/*" {
  capabilities = ["list"]
}

# ----------------------------------------------------------------------------
# LECTURA — Credenciales ServiceNow
# Paths esperados bajo este prefijo:
#   secret/data/amael/servicenow/instance
#   secret/data/amael/servicenow/user
#   secret/data/amael/servicenow/password
#   secret/data/amael/servicenow/client-id     (opcional, si se usa OAuth)
#   secret/data/amael/servicenow/client-secret (opcional, si se usa OAuth)
# ----------------------------------------------------------------------------
path "secret/data/amael/servicenow/*" {
  capabilities = ["read"]
}

# Listado de metadata bajo servicenow/* (mismo criterio que bitbucket).
path "secret/metadata/amael/servicenow/*" {
  capabilities = ["list"]
}

# ----------------------------------------------------------------------------
# LECTURA — Shared internal secret (auth pod-to-pod Raphael ↔ Camael ↔ backend)
# Mismo valor que consume Raphael; permite validar el Bearer cuando Raphael
# invoca el handoff a Camael.
# ----------------------------------------------------------------------------
path "secret/data/amael/internal-secret" {
  capabilities = ["read"]
}

# ----------------------------------------------------------------------------
# LECTURA — Ollama API key (reservado para cuando llm-adapter exponga auth)
# Camael usa `camael_analyzer.py` (ChatOllama con think=False) y en el futuro
# puede ir contra un adapter protegido con API key.
# ----------------------------------------------------------------------------
path "secret/data/amael/ollama-apikey" {
  capabilities = ["read"]
}

# ----------------------------------------------------------------------------
# DENY EXPLÍCITO — Paths de Raphael
# Por ahora Raphael no tiene paths propios fuera del shared `internal-secret`,
# pero reservamos un prefijo `secret/data/amael/raphael/*` para cuando lo tenga
# (p.ej. tokens de Grafana service account, webhook secrets, etc.).
# ----------------------------------------------------------------------------
path "secret/data/amael/raphael/*" {
  capabilities = ["deny"]
}

path "secret/metadata/amael/raphael/*" {
  capabilities = ["deny"]
}

# ----------------------------------------------------------------------------
# DENY EXPLÍCITO — Paths de Productivity (Google OAuth tokens por usuario)
# Camael JAMÁS debe tocar tokens OAuth de usuarios finales.
# ----------------------------------------------------------------------------
path "secret/data/amael/google-tokens/*" {
  capabilities = ["deny"]
}

path "secret/metadata/amael/google-tokens/*" {
  capabilities = ["deny"]
}

# ============================================================================
# Aplicación (ejecutar desde un pod con vault CLI o port-forward al vault-0)
# ----------------------------------------------------------------------------
#
# # 1) Crear la policy
# vault policy write camael-service \
#   /home/richardx/k8s-lab/Amael-AgenticIA/docs/vault/camael-policy.hcl
#
# # 2) Crear el role en el auth method de Kubernetes
# vault write auth/kubernetes/role/camael-service \
#     bound_service_account_names=camael-sa \
#     bound_service_account_namespaces=amael-ia \
#     policies=camael-service \
#     ttl=1h
#
# # 3) Verificar el role
# vault read auth/kubernetes/role/camael-service
#
# # 4) Verificar la policy
# vault policy read camael-service
#
# # 5) (Opcional) Probar login desde un pod con `camael-sa`
# #    Dentro del pod:
# #    TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
# #    vault write auth/kubernetes/login role=camael-service jwt=$TOKEN
# #    vault kv get secret/amael/bitbucket/app-password
# #    vault kv get secret/amael/servicenow/instance
#
# ============================================================================
