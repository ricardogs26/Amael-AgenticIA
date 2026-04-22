# ============================================================================
# Vault Policy: raphael-service
# ----------------------------------------------------------------------------
# Destinada al ServiceAccount `raphael-sa` (namespace `amael-ia`) que corre
# el agente Raphael (SRE) separado del backend.
#
# Principio: least-privilege. Raphael NO toca credenciales de Bitbucket,
# ServiceNow ni Google OAuth (eso es de Camael y Productivity). Solo necesita:
#   - Un shared secret para autenticar llamadas pod-to-pod contra el backend.
#   - La API key de Ollama por si en el futuro se activa auth en el adapter.
#
# KV v2 => los datos viven en `secret/data/...`, metadata en `secret/metadata/...`.
# Para políticas de solo lectura basta con `secret/data/...`.
# ============================================================================

# ----------------------------------------------------------------------------
# LECTURA — Shared internal secret (auth pod-to-pod Raphael → backend)
# Hoy inyectado vía env `AMAEL_INTERNAL_SECRET`. Al migrar a Vault, Raphael
# lo obtendrá por la Vault Agent / k8s auth en startup.
# ----------------------------------------------------------------------------
path "secret/data/amael/internal-secret" {
  capabilities = ["read"]
}

# ----------------------------------------------------------------------------
# LECTURA — Ollama API key (reservado para cuando el llm-adapter exponga auth)
# Hoy Ollama corre sin auth dentro del cluster, pero dejamos el path listo
# para evitar tener que re-emitir la policy cuando se active.
# ----------------------------------------------------------------------------
path "secret/data/amael/ollama-apikey" {
  capabilities = ["read"]
}

# ----------------------------------------------------------------------------
# DENY EXPLÍCITO — Paths de Camael (Bitbucket + ServiceNow)
# Vault es deny-by-default, pero documentamos la intención para defensa en
# profundidad y para que un operador que lea la policy vea la frontera clara.
# Capabilities vacías = negación total (incluye subpaths por el glob).
# ----------------------------------------------------------------------------
path "secret/data/amael/bitbucket/*" {
  capabilities = ["deny"]
}

path "secret/data/amael/servicenow/*" {
  capabilities = ["deny"]
}

# ----------------------------------------------------------------------------
# DENY EXPLÍCITO — Paths de Productivity (Google OAuth tokens por usuario)
# Raphael no debe ver nunca tokens OAuth de usuarios finales.
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
# vault policy write raphael-service \
#   /home/richardx/k8s-lab/Amael-AgenticIA/docs/vault/raphael-policy.hcl
#
# # 2) Crear el role en el auth method de Kubernetes
# vault write auth/kubernetes/role/raphael-service \
#     bound_service_account_names=raphael-sa \
#     bound_service_account_namespaces=amael-ia \
#     policies=raphael-service \
#     ttl=1h
#
# # 3) Verificar el role
# vault read auth/kubernetes/role/raphael-service
#
# # 4) Verificar la policy
# vault policy read raphael-service
#
# # 5) (Opcional) Probar login desde un pod con `raphael-sa`
# #    Dentro del pod:
# #    TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
# #    vault write auth/kubernetes/login role=raphael-service jwt=$TOKEN
# #    vault kv get secret/amael/internal-secret
#
# ============================================================================
