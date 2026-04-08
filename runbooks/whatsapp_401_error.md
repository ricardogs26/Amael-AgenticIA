# Runbook: WhatsApp Bridge 401 Unauthorized

## Descripción
El servicio `whatsapp-bridge` falla al comunicarse con el backend de Amael-IA con un error `Request failed with status code 401`. Esto indica que el token JWT configurado en el secreto `amael-secrets` ha expirado o es inválido.

## Diagnóstico
- **Logs del Bridge:** Buscar mensajes como `[ERROR] Procesando mensaje de ...: Request failed with status code 401` o `Error buscando conversaciones: Request failed with status code 401`.
- **Origen del Token:** El token se guarda en el secreto `amael-secrets`, llave `jwt-token`.
- **Causa Raíz:** `CONFIG_ERROR` / `AUTH_ERROR`.

## Remediación automática
El agente SRE puede auto-reparar este problema ejecutando la acción `REFRESH_WA_TOKEN`. 

Esta acción realiza los siguientes pasos de forma segura:
1. Lee la `JWT_SECRET_KEY` del secreto `google-auth-secret`.
2. Genera un nuevo token JWT para el usuario `bot-amael@richardx.dev`.
3. Actualiza el secreto `amael-secrets` con el nuevo token (codificado en base64).
4. Realiza un `rollout restart` del deployment `whatsapp-bridge-deployment`.

## Verificación
Tras la remediación, los logs del `whatsapp-bridge` deben mostrar éxito en las llamadas a `/api/conversations` y el bot debe responder a los mensajes en WhatsApp.
