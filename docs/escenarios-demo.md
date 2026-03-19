# Escenarios de Demo — Amael-AgenticIA

Guía de referencia para demostrar el sistema en vivo. Todos los prompts están listos
para pegar en el chat de la plataforma (`https://amael-ia.richardx.dev`) o en WhatsApp.

---

## Tabla de contenidos

1. [Parte 1 — Gabriel: Agente DevOps](#parte-1--gabriel-agente-devops)
2. [Parte 2 — Raphael: Agente SRE](#parte-2--raphael-agente-sre)
3. [Parte 3 — Sandalphon: Agente de Investigación](#parte-3--sandalphon-agente-de-investigación)
4. [Parte 4 — Haniel: Agente de Productividad](#parte-4--haniel-agente-de-productividad)
5. [Parte 5 — Runbooks de Raphael](#parte-5--runbooks-de-raphael)
6. [Parte 6 — Guión de Demo (10 minutos)](#parte-6--guión-de-demo-10-minutos)

---

## Parte 1 — Gabriel: Agente DevOps

Gabriel es el agente de desarrollo autónomo. Opera en dos modos:

- **Conversacional** (default): responde preguntas de código con RAG + LLM.
- **Autónomo**: ciclo completo — analiza la tarea → lee el archivo en GitHub → genera el
  código → crea una rama → hace un commit → abre un PR → notifica por WhatsApp.

Gabriel detecta el modo autónomo automáticamente cuando el prompt contiene frases como
"crea un PR", "abre un pull request", "aplica el fix y crea la rama", etc.

---

### Escenario 1.1 — Fix de bug en frontend (CSS overflow)

**Prompt exacto:**

```
Gabriel, hay un bug visual en el frontend-next. En el componente
src/components/ChatMessage.tsx, los mensajes muy largos desbordan el contenedor
lateral y el scroll horizontal aparece en mobile. Aplica el fix con overflow-wrap: break-word
y max-width: 100% en el elemento del mensaje, y crea un PR en ricardogs26/amael-ia
con una descripción clara del problema.
```

**Qué hace Gabriel, paso a paso:**

1. Detecta palabras clave "crea un PR" → activa modo autónomo.
2. Llama al LLM para analizar la tarea y determinar:
   - `target_file`: `src/components/ChatMessage.tsx`
   - `branch_name`: `fix/gabriel-chat-message-overflow`
   - `commit_message`: `fix(ui): prevent long message overflow on mobile`
   - `pr_title`: `fix(ChatMessage): overflow-wrap y max-width para mobile`
3. Lee el contenido actual del archivo desde la API de GitHub (rama `main`).
4. Genera el archivo modificado con el LLM (solo el contenido puro, sin markdown).
5. Crea la rama `fix/gabriel-chat-message-overflow` desde `main`.
6. Hace el commit con el nuevo contenido.
7. Abre el PR contra `main`.
8. Envía una notificación WhatsApp al admin con el número y URL del PR.

**Resultado esperado:**

- Rama creada: `fix/gabriel-chat-message-overflow`
- Commit: hash corto (ej. `a3f1b2c`)
- PR abierto: `https://github.com/ricardogs26/amael-ia/pull/<N>`
- Notificación WhatsApp: "Gabriel completó tarea autónoma (Xs) • PR #N: <url>"

**Tiempo aproximado:** 30–60 segundos (dependiendo de latencia de Ollama y GitHub API).

---

### Escenario 1.2 — Agregar un endpoint nuevo a una API

**Prompt exacto:**

```
Gabriel, agrega un endpoint GET /api/health/detailed al archivo
interfaces/api/routers/chat.py del repositorio ricardogs26/Amael-AgenticIA.
El endpoint debe devolver un JSON con el estado de PostgreSQL, Redis, Qdrant y Ollama,
consultando cada dependencia con un timeout de 2 segundos. Si alguna falla, la respuesta
HTTP debe ser 503. Crea el PR con título "feat: health check granular por dependencia".
```

**Qué hace Gabriel, paso a paso:**

1. Detecta "Crea el PR" → modo autónomo.
2. Analiza la tarea: identifica que el archivo objetivo ya está indicado en el prompt
   (`interfaces/api/routers/chat.py`), lo usa directamente sin pedir al LLM que lo infiera.
3. Lee el router actual de GitHub.
4. Genera el nuevo contenido añadiendo el endpoint `/api/health/detailed` con checks
   asyncronos para cada dependencia y manejo de 503.
5. Crea rama `feat/gabriel-health-check-granular`, commit, PR.

**Resultado esperado:**

- Rama: `feat/gabriel-health-check-granular`
- PR con descripción detallando el contrato del endpoint y la lógica de 503.

**Tiempo aproximado:** 45–75 segundos.

---

### Escenario 1.3 — Actualizar dependencias en pyproject.toml

**Prompt exacto:**

```
Gabriel, en el repositorio ricardogs26/Amael-AgenticIA, actualiza pyproject.toml
para subir langchain de 0.2.x a 0.3.x y langchain-ollama de 0.1.x a 0.2.x.
Asegúrate de que los rangos de versión sean compatibles (usa >= y <).
Crea un PR llamado "chore: actualizar langchain a 0.3.x".
```

**Qué hace Gabriel, paso a paso:**

1. Identifica `pyproject.toml` como archivo objetivo.
2. Lee el archivo actual de GitHub para ver los rangos de versión existentes.
3. Modifica únicamente las líneas de `langchain` y `langchain-ollama` preservando la
   estructura del archivo.
4. Crea rama `chore/gabriel-actualizar-langchain-0-3`, commit, PR.

**Resultado esperado:**

- Solo se modifican las versiones de las dependencias indicadas.
- El resto del `pyproject.toml` queda intacto.

**Tiempo aproximado:** 30–45 segundos.

---

### Escenario 1.4 — Agregar logging a una función existente

**Prompt exacto:**

```
Gabriel, en el archivo agents/researcher/rag_retriever.py del repositorio
ricardogs26/Amael-AgenticIA, agrega logging estructurado a la función retrieve_documents.
Debe loguear: (1) el user_email y la query al inicio, (2) el número de chunks encontrados
antes y después del filtrado, (3) el número de resultados finales. Usa el logger existente
del módulo. Crea un PR descriptivo.
```

**Qué hace Gabriel, paso a paso:**

1. Lee el archivo `rag_retriever.py` desde GitHub.
2. Identifica las funciones `retrieve_documents`, `_detect_filename_filter` y los puntos
   clave donde añadir las llamadas a `logger.info()` / `logger.debug()`.
3. Genera el archivo completo modificado con los logs añadidos.
4. Crea rama `feat/gabriel-logging-rag-retriever`, commit, PR.

**Resultado esperado:**

- Logging añadido en los puntos correctos sin alterar la lógica de negocio.
- PR con descripción indicando qué se loguea y en qué nivel.

**Tiempo aproximado:** 35–55 segundos.

---

### Escenario 1.5 — Crear un nuevo componente React

**Prompt exacto:**

```
Gabriel, en el repositorio ricardogs26/amael-ia, crea el archivo
src/components/AgentStatusBadge.tsx. Es un componente React que recibe una prop
"agent" (string: "gabriel", "raphael", "sandalphon", "haniel") y muestra un badge
con el nombre del agente y un punto de color (verde si activo, gris si no).
Usa Tailwind CSS. Abre un PR con descripción completa.
```

**Qué hace Gabriel, paso a paso:**

1. El archivo no existe aún — Gabriel detecta que debe crear el archivo desde cero
   (el LLM recibe contenido vacío como punto de partida).
2. Genera un componente TypeScript completo con props tipadas, lógica de color por agente
   y estilos Tailwind.
3. Crea rama `feat/gabriel-agent-status-badge`, commit con el nuevo archivo, PR.

**Resultado esperado:**

- Archivo `src/components/AgentStatusBadge.tsx` creado con TypeScript correcto.
- PR con preview del componente en la descripción.

**Tiempo aproximado:** 40–60 segundos.

---

### Escenario 1.6 — Fix de un test que está fallando

**Prompt exacto:**

```
Gabriel, el test tests/unit/agents/test_planner.py en ricardogs26/Amael-AgenticIA
está fallando porque la función test_plan_step_limit no importa correctamente
MAX_PLAN_STEPS desde core.constants. El import actual dice "from agents.planner.agent
import MAX_PLAN_STEPS" pero esa constante se movió a core.constants. Corrige el import
en el archivo de test y crea un PR con título "fix(tests): corregir import MAX_PLAN_STEPS".
```

**Qué hace Gabriel, paso a paso:**

1. Lee el archivo de test desde GitHub.
2. Localiza el import incorrecto y lo reemplaza por `from core.constants import MAX_PLAN_STEPS`.
3. Verifica que no haya otros imports rotos en el mismo archivo.
4. Genera el archivo corregido completo.
5. Crea rama `fix/gabriel-test-import-max-plan-steps`, commit, PR.

**Resultado esperado:**

- Solo se modifica la línea del import (cambio mínimo).
- PR claro indicando la causa raíz: la constante fue movida de módulo.

**Tiempo aproximado:** 25–40 segundos.

---

### Modo conversacional (sin PR)

Para preguntas técnicas sin acción en GitHub, omitir frases como "crea un PR" o "aplica el fix":

```
Gabriel, ¿cuál es la diferencia entre usar ThreadPoolExecutor y asyncio.gather
para paralelizar los steps del executor? ¿Cuál es más adecuado para nuestro stack?
```

Gabriel responde con análisis técnico usando el contexto RAG del proyecto (si hay
documentos indexados) y conocimiento del modelo, sin tocar GitHub.

---

## Parte 2 — Raphael: Agente SRE

Raphael es el agente de Site Reliability Engineering. Funciona en dos modalidades:

- **WhatsApp**: comandos `/sre <cmd>` enviados al bridge de WhatsApp.
- **Chat**: preguntas sobre el estado del cluster vía el pipeline LangGraph.

El loop autónomo de Raphael corre cada 60 segundos sin intervención humana
(Observe → Detect → Diagnose → Decide → Act → Report).

---

### Comandos WhatsApp

Enviar al número de WhatsApp configurado. El bridge los enruta a `POST /api/sre/command`
en el k8s-agent con autenticación interna.

---

#### Escenario 2.1 — Ver estado del loop SRE

**Comando:**

```
/sre status
```

**Qué hace Raphael:**

Consulta el estado runtime del loop autónomo y responde con:

- Estado del loop: activo / detenido
- Circuit breaker: cerrado (normal) / abierto (loop pausado por errores consecutivos)
- Ventana de mantenimiento: activa con minutos restantes / inactiva
- Número de SLOs monitoreados
- Última ejecución del loop

**Respuesta esperada:**

```
SRE Loop: ACTIVO
Circuit Breaker: CERRADO (normal)
Mantenimiento: No activo
SLOs monitoreados: 3
Último ciclo: hace 12s
```

---

#### Escenario 2.2 — Ver últimos incidentes

**Comando:**

```
/sre incidents
```

**Qué hace Raphael:**

Consulta la tabla `sre_incidents` en PostgreSQL y devuelve los 5 incidentes más recientes.

**Respuesta esperada:**

```
Ultimos 5 incidentes:

1. [2026-03-18 09:14] CRASH_LOOP — amael-agentic-deployment
   Accion: ROLLOUT_RESTART | Confianza: 0.85

2. [2026-03-17 22:31] HIGH_MEMORY — productivity-service-deployment
   Accion: ROLLOUT_RESTART | Confianza: 0.72

3. [2026-03-17 14:05] HIGH_ERROR_RATE — amael-agentic-deployment
   Accion: NOTIFY_HUMAN | Confianza: 0.61
...
```

---

#### Escenario 2.3 — Ver postmortems

**Comando:**

```
/sre postmortems
```

**Qué hace Raphael:**

Devuelve los 3 postmortems más recientes generados por el LLM (tabla `sre_postmortems`
en PostgreSQL, creados por `_generate_postmortem()` después de que una verificación
post-acción confirme que el incidente fue resuelto).

**Respuesta esperada:**

```
Postmortems recientes:

1. [2026-03-17] CrashLoop en amael-agentic-deployment
   Causa: dependencia PostgreSQL no disponible en arranque.
   Accion tomada: rollout restart. Resolucion: 4 min.
   Prevencion: agregar readiness probe mas agresiva en postgres.

2. [2026-03-16] OOMKilled en productivity-service
   Causa: procesamiento de bandeja de Gmail con >500 emails.
   Accion tomada: rollout restart. Resolucion: 2 min.
   Prevencion: implementar paginacion en get_emails (max 50/llamada).
```

---

#### Escenario 2.4 — Ver SLOs

**Comando:**

```
/sre slo
```

**Qué hace Raphael:**

Devuelve los objetivos de nivel de servicio configurados con sus targets de disponibilidad.

**Respuesta esperada:**

```
SLO Targets:

/api/chat        → 99.5% disponibilidad (ventana 24h)
/api/k8s-agent   → 99.0% disponibilidad (ventana 24h)
/api/conversations.* → 99.0% disponibilidad (ventana 24h)
```

---

#### Escenario 2.5 — Activar ventana de mantenimiento (30 min)

**Comando:**

```
/sre maintenance on 30
```

**Qué hace Raphael:**

- Escribe una clave en Redis con TTL de 1800 segundos.
- El loop autónomo detecta la clave activa y salta el ciclo (resultado `maintenance`
  en métricas Prometheus).
- Util para deployments planeados: evita que Raphael haga rollout restart de un pod
  que está siendo actualizado intencionalmente.

**Respuesta esperada:**

```
Mantenimiento activado por 30 minutos.
El loop SRE no ejecutara acciones automaticas hasta las 10:45.
```

---

#### Escenario 2.6 — Desactivar ventana de mantenimiento

**Comando:**

```
/sre maintenance off
```

**Qué hace Raphael:**

Elimina la clave de mantenimiento en Redis. El siguiente ciclo del loop (máximo 60s)
ya opera normalmente.

**Respuesta esperada:**

```
Mantenimiento desactivado. El loop SRE retoma operacion normal.
```

---

#### Escenario 2.7 — Pedir ayuda

**Comando:**

```
/sre ayuda
```

**Respuesta esperada:**

```
Comandos SRE disponibles:

/sre status           — Estado del loop y circuit breaker
/sre incidents        — Ultimos 5 incidentes
/sre postmortems      — Ultimos 3 postmortems
/sre slo              — Objetivos de nivel de servicio
/sre maintenance on N — Activar mantenimiento N minutos
/sre maintenance off  — Desactivar mantenimiento
/sre ayuda            — Esta lista
```

---

### Escenarios de chat (interfaz web)

Estos prompts van al chat normal de la plataforma y son enrutados al agente SRE
por el `AgentRouter` (detecta palabras clave: "anomalías", "circuit breaker", "CrashLoop",
"error budget", etc.).

---

#### Escenario 2.8 — Anomalías en las últimas 24 horas

**Prompt exacto:**

```
¿Qué anomalías detectó Raphael en las últimas 24 horas?
```

**Qué hace el sistema:**

1. El router detecta intención SRE → despacha a `SREAgent`.
2. El agente consulta `sre_incidents` en PostgreSQL filtrando por `detected_at > now() - 24h`.
3. Agrupa por tipo de anomalía y cuenta ocurrencias.
4. Responde con un resumen estructurado.

**Resultado esperado:**

Listado de anomalías con tipo, deployment afectado, severidad, acción tomada y resultado
de la verificación post-acción.

---

#### Escenario 2.9 — Estado del circuit breaker

**Prompt exacto:**

```
¿Cuál es el estado del circuit breaker del loop SRE?
```

**Resultado esperado:**

El agente consulta el endpoint `GET /api/sre/loop/status` del k8s-agent y devuelve:

- Estado: CERRADO (operando) / ABIERTO (pausado)
- Contador de errores consecutivos
- Umbral configurado para apertura
- Tiempo estimado de recuperación (si está abierto)

---

#### Escenario 2.10 — Pods en CrashLoop

**Prompt exacto:**

```
¿Hay pods en CrashLoop en el cluster ahora mismo?
```

**Qué hace el sistema:**

1. El agente SRE llama al k8s-agent (`POST /api/k8s-agent`) con la consulta.
2. El k8s-agent ejecuta `kubectl get pods -A` y filtra por estado `CrashLoopBackOff`.
3. Devuelve el estado actual con nombre de pod, namespace, conteo de reinicios y
   último mensaje de error.

**Resultado esperado:**

Estado actual de todos los pods del cluster. Si no hay CrashLoops: "Todos los pods
están en Running. No se detectan CrashLoops en este momento."

---

#### Escenario 2.11 — Error budget de /api/chat

**Prompt exacto:**

```
¿Cuál es el error budget restante de /api/chat?
```

**Qué hace el sistema:**

1. El agente consulta `GET /api/sre/slo/status` en el k8s-agent.
2. El k8s-agent calcula el burn rate de errores en la ventana de 24h usando
   Prometheus (`rate(http_requests_total{handler="/api/chat",status=~"5.."}[24h])`).
3. Compara contra el target de 99.5% disponibilidad.

**Resultado esperado:**

```
SLO /api/chat: target 99.5%
Disponibilidad actual (24h): 99.87%
Error budget consumido: 26% del budget disponible
Estado: SALUDABLE — no hay burn rate acelerado
```

---

## Parte 3 — Sandalphon: Agente de Investigación

Sandalphon maneja RAG sobre los documentos indexados del usuario y búsqueda web
vía DuckDuckGo. El sistema usa RAG automáticamente cuando la query tiene palabras
que coincidan con filenames indexados; de lo contrario, cae al pipeline general.

---

### Escenario 3.1 — Buscar en documentos indexados

**Prerequisito:** haber subido un documento via `POST /api/ingest` (PDF, TXT, DOCX o MD).

**Prompt exacto (con documento "reporte-q1.pdf" indexado):**

```
¿Qué dice el reporte del Q1 sobre los objetivos de crecimiento para el segundo trimestre?
```

**Qué hace Sandalphon:**

1. `_detect_filename_filter()` detecta "reporte" como palabra significativa (>3 chars,
   no stopword) y la compara contra los filenames indexados en Qdrant.
2. Encuentra match con `reporte-q1.pdf` → activa filtro por documento.
3. `client.scroll(limit=500)` recupera todos los chunks de esa colección.
4. Filtra por substring `reporte-q1` en `metadata.filename`.
5. Reranking semántico: calcula cosine similarity entre el embedding de la query y los
   embeddings de cada chunk, ordena de mayor a menor.
6. Devuelve los top-5 chunks más relevantes como contexto al LLM.
7. El LLM genera la respuesta citando el contenido del documento.

**Resultado esperado:**

Respuesta precisa basada en el contenido del PDF, con citas textuales o paráfrasis
de los fragmentos más relevantes. Si el documento no contiene la información: "No
encontré esa información en el documento reporte-q1.pdf."

---

### Escenario 3.2 — Búsqueda web + síntesis

**Prompt exacto:**

```
¿Cuáles son las mejores prácticas actuales para implementar rate limiting
distribuido con Redis en FastAPI? Busca información actualizada.
```

**Qué hace Sandalphon:**

1. No detecta filenames en la query → sin filtro de documento.
2. El Planner genera un step `WEB_SEARCH` + `REASONING`.
3. Sandalphon ejecuta `web_search()` vía DuckDuckGo (3–5 resultados).
4. El resultado se pasa al REASONING step donde el LLM sintetiza la información
   con el contexto del stack tecnológico de Amael-IA.

**Resultado esperado:**

Síntesis de 300–500 palabras con las mejores prácticas actualizadas, ejemplos de código
adaptados al stack FastAPI + Redis del proyecto, y referencias a los artículos encontrados.

---

### Escenario 3.3 — Preguntas sobre arquitectura del sistema

**Prompt exacto:**

```
¿Cómo funciona el sistema de filtrado por documento en el RAG retriever?
¿Por qué se usa scroll en lugar de Qdrant MatchText?
```

**Qué hace Sandalphon:**

El Planner determina que es una pregunta de arquitectura. Si hay documentación
del proyecto indexada (CLAUDE.md, etc.), ejecuta RAG. De lo contrario, el REASONING
step responde con el conocimiento del LLM sobre el código.

**Resultado esperado:**

Explicación técnica del flujo: `_detect_filename_filter()` → `client.scroll(limit=500)` →
filtro Python por substring → reranking cosine similarity. Justificación de por qué
`MatchText` no se usa (requiere índice FTS en Qdrant que no está configurado).

---

### Escenario 3.4 — Tipo de cambio (fast-path)

**Prompt exacto:**

```
¿A cuánto está el dólar hoy?
```

**Qué hace Sandalphon:**

Detecta la query como tipo de cambio → fast-path directo a la API de tipo de cambio
(sin DuckDuckGo). Respuesta en <2 segundos.

**Resultado esperado:**

```
Tipo de cambio USD/MXN al 18 de marzo 2026: $17.23 pesos por dólar.
(Fuente: Banxico / exchangerate-api)
```

---

## Parte 4 — Haniel: Agente de Productividad

Haniel gestiona Google Calendar y Gmail usando tokens OAuth almacenados en Vault.
**Prerequisito:** el usuario debe haber completado el flujo OAuth de Google
(`GET /api/auth/google`) para que sus credenciales estén en Vault.

---

### Escenario 4.1 — Organizar el día

**Prompt exacto:**

```
Haniel, organiza mi día de hoy. ¿Qué reuniones tengo y qué emails importantes
debo atender primero?
```

**Qué hace Haniel:**

1. Recupera los tokens OAuth del usuario desde Vault
   (`secret/data/amael/google-tokens/<user_email>`).
2. Llama a Google Calendar API: eventos de hoy con hora, título y descripción.
3. Llama a Gmail API: emails no leídos de las últimas 24h, ordenados por importancia.
4. El LLM sintetiza un plan del día priorizando reuniones y emails críticos.

**Resultado esperado:**

```
Plan del dia — Martes 18 de marzo:

REUNIONES:
- 10:00 Daily standup (30 min) — Google Meet
- 14:00 Review de arquitectura (1h) — equipo backend

EMAILS PENDIENTES (3 sin leer):
- [URGENTE] Deploy fallido en produccion — DevOps Lead
- Revision de PR #42 — GitHub
- Factura de servicios cloud — Proveedor

RECOMENDACION: atender el email de deploy antes del standup.
```

---

### Escenario 4.2 — Ver eventos de la semana

**Prompt exacto:**

```
¿Qué reuniones tengo programadas esta semana? Muéstrame el calendario de lunes a viernes.
```

**Qué hace Haniel:**

Consulta Google Calendar API con rango `timeMin=lunes` / `timeMax=viernes` de la semana
actual. Lista todos los eventos con hora, duración y ubicación/link.

**Resultado esperado:**

Tabla o lista de eventos por día con hora de inicio, duración y descripción.

---

### Escenario 4.3 — Revisar emails no leídos

**Prompt exacto:**

```
¿Qué emails importantes tengo sin leer? Prioriza los que requieren acción inmediata.
```

**Qué hace Haniel:**

1. Llama a Gmail API con query `is:unread` y filtro de últimas 48h.
2. Recupera asunto, remitente y snippet de cada email.
3. El LLM clasifica por urgencia (acción inmediata / informativo / puede esperar).

**Resultado esperado:**

Lista priorizada de emails con remitente, asunto y acción recomendada para cada uno.

---

### Escenario 4.4 — Verificar estado de credenciales OAuth

**Prompt exacto:**

```
Haniel, ¿mis credenciales de Google están configuradas correctamente?
¿El token de OAuth sigue siendo válido?
```

**Qué hace Haniel:**

Ejecuta `action: credentials_status`. Consulta Vault para verificar que el token exista
y verifica con la API de Google si sigue siendo válido (o si necesita refresh).

**Resultado esperado:**

```
Credenciales OAuth: CONFIGURADAS
Token de acceso: valido (expira en 45 min)
Refresh token: disponible (renovacion automatica activa)
Scopes activos: calendar.readonly, gmail.readonly
```

Si las credenciales no están: "No encontre credenciales OAuth para tu cuenta.
Completa el flujo de autorizacion en: https://amael-ia.richardx.dev/api/auth/google"

---

### Escenario 4.5 — Day Planner (CronJob automático)

Este escenario no requiere intervención — ocurre automáticamente.

**Qué pasa cada lunes a viernes a las 7:00am (hora Ciudad de México):**

1. El CronJob `day-planner` ejecuta `POST /api/planner/daily` en el backend.
2. Haniel recupera el calendario del día y los emails no leídos de madrugada.
3. Genera el plan del día en formato WhatsApp-friendly.
4. El backend envía el mensaje al bridge de WhatsApp (`POST /send`).
5. El usuario recibe el resumen de su día en WhatsApp antes de comenzar a trabajar.

---

## Parte 5 — Runbooks de Raphael

### ¿Qué son los runbooks?

Los runbooks son archivos Markdown estructurados que contienen el conocimiento operativo
para diagnosticar y remediar problemas específicos de infraestructura. Raphael los usa como
base de conocimiento durante el diagnóstico con LLM: cuando detecta una anomalía, busca
el runbook más relevante y lo incluye como contexto para que el modelo genere un diagnóstico
más preciso y accionable.

Los archivos fuente viven en:
`/home/richardx/k8s-lab/Amael-IA/k8s-agent/runbooks/`

---

### Los 9 runbooks existentes

#### 1. `crash_loop.md` — CrashLoopBackOff

Cubre los 5 escenarios más comunes de CrashLoop en Amael-IA:

- **Dependencia caída** (causa #1): conexión fallida a PostgreSQL, Redis, Qdrant u Ollama
  en el arranque. Síntoma: `ECONNREFUSED` en logs. Remediación: esperar que la dependencia
  sane, luego rollout restart.
- **Variable de entorno faltante**: `ValueError: missing required env`. Remediación:
  corregir ConfigMap/Secret y hacer rollout restart.
- **Bug en arranque**: `ImportError`, `ModuleNotFoundError`, `Traceback` en logs.
  Remediación: nueva imagen con el bug corregido (rollout restart no ayuda).
- **OOMKilled en arranque**: pod muere antes de estar Ready. Ver runbook `oom_killed.md`.
- **Liveness probe agresiva**: el pod arranca lento y es matado antes de estar listo.
  Remediación: aumentar `initialDelaySeconds`.

Limit de auto-remediación: máximo 3 reinicios automáticos en 15 minutos; después escalar a humano.

#### 2. `oom_killed.md` — OOMKilled (Out of Memory)

Documenta las 4 causas de OOM en Amael-IA:

- **Carga inusual** (productivity-service): Gmail/Calendar procesa volumen grande.
  Memory limit actual: 512Mi. Remediación: subir a 1Gi + añadir paginación.
- **Contexto LLM masivo** (backend, k8s-agent): conversación extremadamente larga.
  Remediación: rollout restart + revisar `MAX_CONTEXT_CHARS`.
- **Memory leak acumulativo**: memoria crece gradualmente en horas. Diagnosticar con
  `container_memory_working_set_bytes` en Prometheus.
- **Qdrant/Redis con datos masivos**: rollout restart del pod afectado.

Incluye PromQL para diagnóstico de uso de memoria vs limits.

#### 3. `image_pull_error.md` — ImagePullBackOff / ErrImagePull

Cubre las 4 causas de error de imagen:

- **Tag incorrecto**: la versión especificada no existe en `registry.richardx.dev`.
- **Registry inaccesible**: timeout de conexión al registry privado.
- **Credenciales expiradas**: imagePullSecret inválido.
- **Disco lleno**: el nodo no puede escribir la imagen descargada.

Esta anomalía **siempre requiere intervención manual**. Raphael notifica pero no puede
corregir automáticamente un tag de imagen incorrecto.

#### 4. `pending_pod.md` — Pod en estado Pending (FailedScheduling)

Las 4 causas de pods Pending en Amael-IA:

- **GPU no disponible** (causa principal — Ollama): solo hay 1 GPU (RTX 5070).
  Nota crítica: para reiniciar Ollama usar `kubectl delete pod -l app=ollama`,
  NO `rollout restart` (crearía un segundo pod Pending).
- **Recursos insuficientes**: CPU o RAM del nodo al límite.
- **PVC no disponible**: volumen no existe o está bound a otro pod.
- **Node selector no satisfecho**: ningún nodo cumple los tolerations requeridos.

#### 5. `node_not_ready.md` — Nodo NotReady

El más crítico para un cluster single-node: `lab-home` en estado NotReady significa
que el cluster completo está degradado.

Causas: kubelet caído, nodo reiniciado, disco lleno, red caída, OOM del sistema operativo.

Remediación completa documentada:
1. SSH al nodo `lab-home`
2. `sudo snap restart microk8s`
3. Después del reinicio: dessellar Vault con 3 de las 5 claves Shamir

Raphael no puede actuar remotamente en este escenario — solo notifica con urgencia máxima.

#### 6. `high_restarts.md` — High Restart Rate

Para pods que acumulan muchos reinicios (≥5) pero siguen en Running — indica inestabilidad
crónica sin CrashLoop.

Causas: liveness probe fallando intermitentemente bajo carga, dependencia intermitente,
memory leak lento (OOM cada X horas), timeout de LLM en health check.

Remediación automática: rollout restart. Si vuelve a reiniciarse en <15 min → escalar
a humano para investigar causa raíz.

#### 7. `disk_pressure.md` — DiskPressure en Nodo

Cuando el espacio disponible cae bajo el umbral de Kubernetes. Principales consumidores
en Amael-IA: imágenes Docker acumuladas, logs de pods, modelos de Ollama (qwen2.5:14b ≈ 9GB),
datos de PostgreSQL/Qdrant/MinIO, snapshots de MicroK8s.

Incluye script de limpieza de emergencia. Raphael no puede ejecutarlo directamente
(sin acceso SSH) — notifica al humano con el script listo para copiar/pegar.

#### 8. `grafana_no_data.md` — Dashboards de Grafana sin datos

Explica por qué los dashboards 1, 2, 5, 6 y 7 muestran "No data" después de un
reinicio del backend: los counters Prometheus se resetean a cero y solo se incrementan
con tráfico activo. Incluye árbol de decisión de diagnóstico y PromQL de verificación.

#### 9. `whatsapp_401_error.md` — Error 401 en WhatsApp Bridge

Cubre errores de autenticación entre el bridge de WhatsApp y el backend. Incluye
diagnóstico del token JWT bot y procedimiento de regeneración.

---

### Cómo se indexan los runbooks en Qdrant al arrancar

Al iniciar el k8s-agent (o el `amael-agentic-backend` al llamar a `init_runbooks_qdrant()`),
se ejecuta el siguiente flujo:

1. Se leen todos los archivos `.md` del directorio `runbooks/`.
2. Cada archivo se divide en chunks de texto (si es muy largo) o se procesa completo.
3. Para cada chunk se genera un embedding usando `nomic-embed-text` via Ollama
   (`POST http://ollama-service:11434/api/embeddings`).
4. El embedding se almacena en la colección `sre_runbooks` de Qdrant junto con
   el metadata del archivo (nombre, tipo de incidente).
5. Si la colección ya existe y ya tiene documentos, el indexado se salta
   (para no duplicar en cada reinicio).

**Prerequisito crítico**: el modelo `nomic-embed-text` debe estar descargado en Ollama.
Si no está disponible, los embeddings retornan `[]` silenciosamente y la búsqueda de
runbooks queda desactivada.

---

### Cómo Raphael usa los runbooks durante el diagnóstico

Cuando el loop autónomo detecta una anomalía (ej. `CRASH_LOOP` en `frontend-next`),
el flujo de diagnóstico es:

```
1. Generar embedding del incidente:
   texto = "CrashLoopBackOff en frontend-next-deployment, 8 reinicios,
            último error: ECONNREFUSED redis-service:6379"
   embedding = nomic-embed-text(texto)

2. Buscar runbooks similares en Qdrant (query_points):
   resultado = qdrant.query_points(
       collection_name="sre_runbooks",
       query=embedding,
       limit=2,
       score_threshold=0.6
   )
   → devuelve fragmentos de crash_loop.md (score=0.91) y oom_killed.md (score=0.63)

3. Construir el prompt para diagnose_with_llm():
   prompt = f"""
   Anomalía detectada: CRASH_LOOP en frontend-next-deployment
   Observaciones: {snapshot_del_pod}

   Runbooks relevantes:
   {contenido_crash_loop_md}

   Basándote en el runbook y las observaciones, determina:
   - Causa raíz más probable (con porcentaje de confianza)
   - Acción recomendada: ROLLOUT_RESTART o NOTIFY_HUMAN
   - Justificación
   """

4. El LLM genera el diagnóstico con confidence score (0.0–1.0).

5. Si confidence > threshold configurado Y acción = ROLLOUT_RESTART:
   → Ejecuta el rollout restart automáticamente.
   → Programa verificación post-acción en 5 minutos.

6. Si confidence < threshold o acción = NOTIFY_HUMAN:
   → Notifica al administrador por WhatsApp con el diagnóstico y la causa raíz.
```

La búsqueda semántica permite que un incidente de "memoria agotada" encuentre
el runbook `oom_killed.md` incluso si el mensaje exacto del error es diferente al texto
del runbook, porque los embeddings capturan similitud semántica, no textual.

---

### Cómo se generan runbooks automáticamente (P4-D)

Después de que Raphael resuelve exitosamente un incidente (verificación post-acción
confirma que el pod está healthy), la función `_maybe_save_runbook_entry()` crea
automáticamente una nueva entrada de runbook:

1. Toma el incidente resuelto: tipo de anomalía, observaciones, diagnóstico del LLM,
   acción tomada y resultado.
2. El LLM genera un fragmento de runbook en formato Markdown estructurado describiendo
   este caso específico.
3. El fragmento se embede con `nomic-embed-text` y se guarda en la colección `sre_runbooks`
   de Qdrant como un nuevo documento.
4. Se registra la métrica `amael_sre_auto_runbook_saved_total`.

Con el tiempo, la base de conocimiento de Raphael crece orgánicamente con casos reales
del cluster, complementando los runbooks manuales iniciales.

---

### Cómo ver qué runbooks hay indexados

**Desde el chat de la plataforma:**

```
Raphael, ¿qué runbooks tienes indexados en tu base de conocimiento?
¿Puedes listar los documentos disponibles en la colección sre_runbooks de Qdrant?
```

**Desde la terminal (acceso directo a Qdrant):**

```bash
# Port-forward a Qdrant
kubectl port-forward svc/qdrant-service 6333:6333 -n amael-ia

# Listar todos los puntos de la colección sre_runbooks
curl http://localhost:6333/collections/sre_runbooks/points/scroll \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"limit": 100, "with_payload": true, "with_vector": false}' | jq '.result.points[].payload.source'
```

**Desde Prometheus (contar runbooks auto-generados):**

```promql
amael_sre_auto_runbook_saved_total
```

---

## Parte 6 — Guión de Demo (10 minutos)

Guión paso a paso para demostrar el sistema en vivo ante un audience técnico o de negocio.

**Prerequisitos antes de la demo:**
- `kubectl get pods -n amael-ia` — todos los pods en Running.
- Grafana abierto en `https://amael-ia.richardx.dev/grafana` (o port-forward).
- Chat de la plataforma abierto en `https://amael-ia.richardx.dev`.
- WhatsApp conectado al bridge (QR escaneado).
- Vault unsealed.

---

### Minuto 0–2: Dashboard de Grafana

**Acción:** Abrir el dashboard "Amael - SRE Autónomo" (UID: `amael-sre-agent`) en Grafana.

**Mostrar y explicar:**

1. Panel "Loop runs by result": línea verde de `ok_clean` y `ok` — el loop lleva corriendo
   sin interrupciones. Si hay picos de `error`, señalarlos como contexto.

2. Panel "Anomalías detectadas": si hay barras de `CRASH_LOOP`, `HIGH_MEMORY`, etc.,
   describir el último incidente detectado y la acción tomada automáticamente.

3. Panel "Diagnosis confidence p90": la línea muestra que el LLM diagnóstica con >0.7
   de confianza en el p90 — suficiente para acciones automáticas.

4. Panel "Circuit Breaker": CERRADO (verde) — el loop está activo y operando.

**Frase clave para el audience:**
"Este dashboard muestra el trabajo autónomo de Raphael. Cada 60 segundos observa el
cluster, detecta anomalías y decide si actuar o escalar. Lleva N ciclos corriendo sin
intervención humana."

---

### Minuto 2–5: Gabriel en acción

**Acción:** Pegar el siguiente prompt en el chat de la plataforma:

```
Gabriel, en el repositorio ricardogs26/Amael-AgenticIA, el archivo
observability/metrics.py tiene un contador llamado "http_requests_counter"
pero el estándar del proyecto es "http_requests_total" (con sufijo _total para
contadores Prometheus). Renombra el contador en ese archivo y crea un PR con título
"fix(metrics): estandarizar nombre de contador HTTP a http_requests_total".
```

**Mientras Gabriel trabaja (30–60 segundos), narrar:**

1. "Gabriel detectó 'crea un PR' → activó modo autónomo."
2. "Ahora está analizando la tarea con el LLM para determinar la rama, el commit
   message y el PR title."
3. "Lee el archivo actual de GitHub para ver el contenido real antes de modificarlo."
4. "Genera el archivo completo modificado — solo la línea del nombre del contador
   cambia, el resto permanece intacto."
5. "Crea la rama, hace el commit, abre el PR."

**Cuando aparezca el resultado:** mostrar el PR URL en el chat y navegar a GitHub.

---

### Minuto 5–7: El PR en GitHub

**Acción:** Abrir el PR generado en GitHub (URL del resultado anterior).

**Mostrar:**

1. Rama: `fix/gabriel-estandarizar-contador-http-total` — creada automáticamente.
2. Commit: mensaje claro en formato convencional (`fix(metrics): ...`).
3. Diff: exactamente un cambio — solo la línea del nombre del contador, nada más.
4. Descripción del PR: generada por el LLM, explica la causa raíz (nombre no estándar),
   el impacto (dashboards Prometheus usan `_total` por convención) y el fix aplicado.

**Frase clave:**
"Gabriel no generó código al azar — leyó el archivo real de GitHub, aplicó el cambio
mínimo necesario y documentó el razonamiento en el PR. Todo en menos de un minuto."

---

### Minuto 7–9: CI/CD y deploy

**Acción:** En GitHub, hacer merge del PR (o mostrar que hay un CI corriendo si está configurado).

**Si hay GitHub Actions configurado:**

1. Mostrar el workflow corriendo: lint con Ruff, tests con pytest.
2. Si el pipeline pasa: mostrar el build de la imagen Docker hacia `registry.richardx.dev`.
3. Si hay ArgoCD o un webhook de deploy: mostrar el rolling update del deployment.

**Si no hay CI completo configurado (alternativa):**

```bash
# Mostrar que el manifiesto de K8s sería el siguiente paso
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia
```

**Frase clave:**
"El ciclo completo es: chat → Gabriel → PR → CI → deploy. Todo auditable en git."

---

### Minuto 9–10: Raphael en WhatsApp

**Acción:** Enviar desde WhatsApp:

```
/sre status
```

**Mientras llega la respuesta, narrar:**
"Este comando llega al bridge de WhatsApp, que lo enruta al k8s-agent con autenticación
interna. Raphael consulta el estado del loop, el circuit breaker y el modo de mantenimiento."

**Cuando llegue la respuesta de WhatsApp, mostrarla en pantalla.**

**Acción de cierre:** enviar un segundo mensaje:

```
/sre incidents
```

**Mostrar los últimos incidentes con su acción tomada.**

**Frase de cierre:**
"Raphael lleva operando de forma autónoma desde que arrancó el cluster. Cada incidente
que ves aquí fue detectado, diagnosticado y resuelto — o escalado — sin intervención
humana. Este es el estado del arte en SRE autónomo con LLM."

---

### Notas para el presentador

- Si Gabriel tarda más de 90s, puede ser latencia de Ollama bajo carga. Normal.
- Si el PR falla por `GITHUB_TOKEN` no configurado: usar un escenario conversacional
  (sin PR) como backup.
- Si Raphael no responde en WhatsApp en <30s: verificar que el bridge esté Running
  con `kubectl get pods -n amael-ia -l app=whatsapp-bridge`.
- Vault debe estar unsealed para que Haniel funcione. Verificar con
  `kubectl port-forward -n vault svc/vault 8200:8200` y `vault status`.
- Los paneles "No data" en Grafana 1, 2, 5, 6, 7 son normales tras un reinicio reciente
  — necesitan tráfico. Enviar un mensaje al chat antes de la demo para activarlos.
