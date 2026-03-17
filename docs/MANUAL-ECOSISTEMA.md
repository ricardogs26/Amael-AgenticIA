# Manual del Ecosistema Amael-AgenticIA

> **Para quién es este manual**: Para el operador/dueño del sistema que quiere entender cómo funciona todo y cómo solicitar cambios o mejoras de forma efectiva.

---

## Tabla de Contenidos

1. [Visión General](#1-visión-general)
2. [Arquitectura del Sistema](#2-arquitectura-del-sistema)
3. [Los Agentes (Familia Angélica)](#3-los-agentes-familia-angélica)
4. [Cómo Fluye una Solicitud](#4-cómo-fluye-una-solicitud)
5. [Infraestructura K8s](#5-infraestructura-k8s)
6. [CI/CD y GitFlow](#6-cicd-y-gitflow)
7. [Observabilidad](#7-observabilidad)
8. [Seguridad y Acceso](#8-seguridad-y-acceso)
9. [Cómo Solicitar Cambios y Mejoras](#9-cómo-solicitar-cambios-y-mejoras)
10. [Referencia Rápida de Endpoints](#10-referencia-rápida-de-endpoints)
11. [Troubleshooting Común](#11-troubleshooting-común)

---

## 1. Visión General

**Amael-AgenticIA** es una plataforma multi-agente modular que vive en un cluster Kubernetes de un solo nodo (`lab-home`, MicroK8s). Es el "cerebro operativo" que:

- Responde preguntas y ejecuta tareas vía chat (WhatsApp y web)
- Monitorea el cluster K8s de forma autónoma (SRE)
- Genera código, hace deploys y gestiona CI/CD
- Administra tu agenda de Google Calendar y Gmail
- Recuerda conversaciones anteriores contigo (memoria episódica)

### Capas del sistema

```
┌────────────────────────────────────────────────────────┐
│  CLIENTES                                              │
│  Frontend Next.js · WhatsApp Bridge · API REST         │
├────────────────────────────────────────────────────────┤
│  GATEWAY                                               │
│  FastAPI + JWT Auth + Rate Limit + CORS                │
├────────────────────────────────────────────────────────┤
│  ORQUESTACIÓN                                          │
│  AgentRouter → AgentDispatcher → LangGraph             │
├─────────────────────┬──────────────────────────────────┤
│  AGENTES (12)       │  SKILLS (7) + TOOLS (5)          │
│  Sariel, Raphael,   │  K8s, LLM, RAG, Vault, Web       │
│  Gabriel, Zaphkiel  │  Prometheus, Grafana, GitHub...   │
│  Jophiel, Camael... │                                  │
├─────────────────────┴──────────────────────────────────┤
│  ALMACENAMIENTO                                        │
│  PostgreSQL · Redis · Qdrant · MinIO                   │
├────────────────────────────────────────────────────────┤
│  INFRAESTRUCTURA                                       │
│  MicroK8s · Ollama (GPU) · Cloudflare Tunnel · Vault   │
└────────────────────────────────────────────────────────┘
```

### URLs públicas

| Servicio | URL |
|---------|-----|
| Chat Web (Next.js) | `https://amael-ia.richardx.dev/` |
| API Backend | `https://amael-ia.richardx.dev/api/` |
| Docs Swagger (solo dev) | `https://amael-ia.richardx.dev/docs` |
| Grafana Dashboards | `https://grafana.richardx.dev/` |
| Métricas Prometheus | `https://amael-ia.richardx.dev/metrics` |

---

## 2. Arquitectura del Sistema

### Componentes principales

```
Amael-AgenticIA/
├── main.py                    # Entry point FastAPI + startup
├── agents/                    # Los 12 agentes especializados
├── skills/                    # Capacidades reutilizables (K8s, LLM, RAG...)
├── tools/                     # Integraciones externas (GitHub, Prometheus...)
├── orchestration/             # LangGraph pipeline + routing
├── interfaces/api/routers/    # 15 endpoints REST
├── storage/                   # Clientes PostgreSQL y Redis
├── security/                  # Validación y sanitización
├── observability/             # Métricas, trazas, logs
├── config/settings.py         # Variables de entorno
└── k8s/                       # Manifests Kubernetes
```

### Modelo de LLM

El LLM que usa el sistema por defecto es **qwen2.5:14b** corriendo localmente en Ollama sobre la GPU RTX 5070 del nodo `lab-home`. Los embeddings usan **nomic-embed-text**.

> Todo el procesamiento de LLM es local — ningún prompt sale a servicios externos de pago.

---

## 3. Los Agentes (Familia Angélica)

El sistema tiene **12 agentes**, cada uno con un nombre de ángel y un rol específico.

### Agentes del Pipeline Principal

Estos tres trabajan siempre juntos en cualquier consulta que pasa por LangGraph:

| Ángel | Rol | Qué hace |
|-------|-----|----------|
| **Sariel** (Planner) | Planificador | Recibe tu pregunta y la convierte en un plan de pasos (máx. 8) |
| **Executor** | Ejecutor | Ejecuta cada paso del plan — herramientas en paralelo, razonamiento en secuencia |
| **Remiel** (Supervisor) | Supervisor de calidad | Califica la respuesta del 0-10; si < 6 pide replanning (máx. 1 reintento) |

### Agentes Especializados (Ruta Directa)

Estos se invocan directamente sin pasar por todo el pipeline:

| Ángel | Nombre técnico | Especialidad | Cuándo se activa |
|-------|---------------|--------------|-----------------|
| **Raphael** | `raphael` | SRE autónomo | Monitoreo 24/7 del cluster, diagnóstico y auto-healing |
| **Haniel** | `haniel` | Productividad | Google Calendar, Gmail, Day Planner |
| **Sandalphon** | `sandalphon` | Investigación | Búsqueda en documentos propios + web |
| **Raziel** | `raziel` | CTO/Estrategia | Decisiones técnicas, roadmap, arquitectura de alto nivel |
| **Gabriel** | `gabriel` | Desarrollador autónomo | Escribe código, crea PRs en GitHub, refactoring autónomo |
| **Uriel** | `uriel` | Arquitecto | Diseño de sistemas, ADRs, diagramas de arquitectura |
| **Zaphkiel** | `zaphkiel` | Memoria | Guarda y recupera contexto de sesiones anteriores |
| **Jophiel** | `jophiel` | Coder en memoria | Genera/analiza/refactoriza código directamente en el chat |
| **Camael** | `camael` | DevOps | CI/CD pipelines, operaciones de entrega, K8s ops |

### Raphael — El SRE Autónomo

Raphael merece explicación especial porque es el único agente que trabaja **sin que lo llames**:

```
Cada 60 segundos:
  Observar → Detectar anomalías → Diagnosticar con LLM
       → Decidir acción → Ejecutar → Reportar → Verificar (5 min después)
```

**Qué puede hacer solo (sin pedirle permiso):**
- Reiniciar pods en CrashLoopBackOff, OOMKilled, fallos
- Hacer rollback automático si un deploy reciente causó problemas

**Qué requiere intervención humana:**
- Problemas de imagen Docker (IMAGE_PULL_ERROR)
- Nodo caído (NODE_NOT_READY)
- CPU alta, errores 5xx, SLO violados

---

## 4. Cómo Fluye una Solicitud

### Flujo completo: pregunta por WhatsApp

```
Tú (WhatsApp)
    ↓
whatsapp-bridge (Express.js)
    ↓ POST /api/chat
    ↓ JWT Bot Token + user_id real
FastAPI /api/chat
    ↓ Validar JWT
    ↓ Rate limit (15 requests/60s por usuario, Redis)
    ↓ validate_prompt() — máx 4000 chars, sin inyección
    ↓ _retrieve_memory_context() → Zaphkiel recupera contexto previo
    ↓ AgentRouter.classify() — ¿a qué agente va esto?
    ↓
  ┌─────────────────────────────────────────────────────────┐
  │ ¿Intent directo? (sre/productivity/research/cto/dev...) │
  │   → AgentRegistry.get(agente) → Agent.run()             │
  │ ¿Pipeline? (general/kubernetes/monitoring/qa...)        │
  │   → LangGraph: Sariel→Executor→Remiel                   │
  └─────────────────────────────────────────────────────────┘
    ↓ respuesta generada
    ↓ sanitize_output() — redacta tokens, JWTs
    ↓ _store_memory_episode() → Zaphkiel guarda la interacción
    ↓
whatsapp-bridge
    ↓ POST /send al bridge
Tú (recibís la respuesta en WhatsApp)
```

### ¿Cómo sabe el sistema a qué agente enviarte?

El **AgentRouter** usa dos mecanismos:

1. **Keyword matching** (rápido, confianza 0.9):
   - "pod", "kubectl", "namespace", "deploy" → `kubernetes`
   - "error", "alerta", "monitoreo", "SRE" → `sre`
   - "código", "función", "refactor", "PR" → `coder`
   - "calendar", "agenda", "gmail", "correo" → `productivity`
   - "busca", "documento", "PDF", "RAG" → `research`
   - "arquitectura", "diseño", "ADR" → `arch`
   - "CI/CD", "pipeline", "deploy", "release" → `devops`
   - "estrategia", "roadmap", "decisión técnica" → `cto`

2. **LLM fallback** (cuando no hay keyword clara):
   El propio LLM clasifica la intención

### Tipos de pasos que puede ejecutar el Executor

| Tipo | Qué hace |
|------|----------|
| `K8S_TOOL` | Llama al k8s-agent para operaciones de Kubernetes |
| `RAG_RETRIEVAL` | Busca en tus documentos indexados (Qdrant) |
| `PRODUCTIVITY_TOOL` | Google Calendar/Gmail vía OAuth en Vault |
| `WEB_SEARCH` | Busca en internet (DuckDuckGo) |
| `REASONING` | El LLM razona sobre la información recopilada |
| `CODE_GENERATION` | Genera código y puede hacer commit en GitHub |
| `TTS_TOOL` | Síntesis de voz (Piper TTS) |

---

## 5. Infraestructura K8s

Todo corre en el namespace `amael-ia` del cluster MicroK8s en `lab-home`.

### Servicios en producción

| Servicio | Imagen | Versión | Puerto | Descripción |
|---------|--------|---------|--------|-------------|
| `amael-agentic-backend` | registry.richardx.dev/amael-agentic-backend | **1.8.1** | 8000 | Backend principal (este repo) |
| `frontend-next` | registry.richardx.dev/frontend-next | 1.3.8 | 3000 | UI Web (activa) |
| `whatsapp-bridge` | registry.richardx.dev/whatsapp-bridge | 1.5.0 | 3000 | Bot de WhatsApp |
| `k8s-agent` | registry.richardx.dev/k8s-agent | 1.6.8 | 8002 | Raphael SRE + K8s tools |
| `productivity-service` | registry.richardx.dev/productivity-service | 1.3.0 | 8001 | Haniel Calendar/Gmail |
| `ollama` | ollama/ollama | latest | 11434 | LLM local (GPU RTX 5070) |
| `piper-service` | registry.richardx.dev/piper-service | 1.0.0 | 8010 | TTS Piper |
| `github-runner` | registry.richardx.dev/github-runner | 1.0.0 | — | Runner CI/CD self-hosted |

### Almacenamiento (StatefulSets)

| Servicio | Puerto interno | Uso |
|---------|---------------|-----|
| `postgres-service` | 5432 | Conversaciones, usuarios, incidentes SRE |
| `redis-service` | 6379 | Rate limiting, caché, deduplicación SRE |
| `qdrant-service` | 6333 | Vectores RAG por usuario + runbooks SRE |
| `minio-service` | 9000 | Backup de documentos subidos |

### Routing de Ingress (amael-ia.richardx.dev)

```
/api/*      → amael-agentic-service:8000   (este backend)
/llm/*      → llm-adapter:80               (proxy OpenAI-compatible a Ollama)
/*          → frontend-next:3000           (UI Next.js)
```

### Vault (Secretos)

HashiCorp Vault corre en el namespace `vault`. Guarda:
- OAuth tokens de Google por usuario (`secret/data/amael/google-tokens/*`)
- Accedido por el backend y productivity-service via Kubernetes auth (JWT del ServiceAccount)

---

## 6. CI/CD y GitFlow

### Ramas

```
develop  ←── rama por defecto
   ↑
   │  Gabriel abre PRs aquí (código autónomo)
   │  Tú desarrollas features aquí
   │
   ↓ PR con aprobación humana (voBo) + Tests pasando
main  ←── protegida, solo PRs aprobados
   ↓
  CI/CD ejecuta deploy automático
```

### Pipeline CI/CD (.github/workflows/ci.yml)

Cada push a `main` o `develop` dispara:

```
Job 1: Tests & Lint (ubuntu-latest, ~2 min)
  → ruff check . (linting)
  → pytest tests/ -m "not e2e" (37+ tests unitarios)

Job 2: Build & Push (self-hosted runner amael-lab, ~8 min)
  → Solo en push a main
  → docker build → push a registry.richardx.dev (red local, sin Cloudflare)

Job 3: Deploy (self-hosted runner amael-lab, ~2 min)
  → Solo en push a main, después de Build exitoso
  → kubectl apply k8s/agents/05-backend-deployment.yaml
  → kubectl rollout status (espera hasta 3 min)
  → Notifica a Raphael (monitoreo intensificado 10 min post-deploy)
```

### Webhook CI (Option B — Camael proactivo)

GitHub notifica a `POST /api/devops/ci-hook` cuando un workflow termina:
- Si `conclusion = failure` → Camael envía notificación por WhatsApp
- Validado con firma HMAC-SHA256 (secret en `amael-secrets/GITHUB_WEBHOOK_SECRET`)

### Reglas de la rama main

Para hacer merge a `main` se requiere:
1. ✅ Status check **"Tests & Lint"** pasando
2. ✅ **1 aprobación** (voBo del dueño del repo)
3. El dueño (ricardogs26) puede hacer push directo (bypass como admin)

---

## 7. Observabilidad

### Grafana — 8 Dashboards

Accesibles en `https://grafana.richardx.dev/`:

| Dashboard | UID | Qué muestra |
|-----------|-----|-------------|
| LLM & HTTP | `amael-llm` | Latencia LLM, requests por endpoint, errores HTTP |
| Pipeline de Agente | `amael-agent` | Pasos del executor, latencia por tipo, quality scores |
| RAG Performance | `amael-rag` | Hits/misses, latencia de búsqueda, chunks recuperados |
| Infraestructura & GPU | `amael-infra` | CPU/memoria por pod, uso GPU, disco |
| Supervisor & Calidad | `amael-supervisor` | Scores Remiel, replanning rate, acceptance rate |
| Seguridad & Rate Limiting | `amael-security` | Rate limit hits, prompts rechazados, auth failures |
| Service Map | `amael-service-map` | Grafo de dependencias entre servicios (OTel) |
| SRE Autónomo | `amael-sre-agent` | Loop SRE, anomalías detectadas, acciones tomadas |

### Trazas distribuidas

Todas las requests generan spans OpenTelemetry que fluyen:
```
amael-agentic-backend → otel-collector → Tempo → Grafana (Explore → Tempo)
```

### Logs

Logs en JSON estructurado con campos `request_id`, `user_id`, `conversation_id`:
```bash
kubectl logs -n amael-ia deploy/amael-agentic-deployment --tail=100 -f
```

---

## 8. Seguridad y Acceso

### Autenticación

Todos los endpoints `/api/*` (excepto `/health`, `/metrics`, `/api/auth/*`) requieren JWT:

```
Google OAuth 2.0 → JWT firmado con JWT_SECRET_KEY
```

El token se envía como `Authorization: Bearer <token>` o cookie de sesión.

### Control de Acceso

Solo usuarios en la whitelist pueden usar el sistema. La whitelist vive en PostgreSQL:
```sql
SELECT user_id FROM user_profile WHERE status = 'active';
```

Usuarios configurados: `ricardogs26@gmail.com`, `rguzmans@bancobase.com`, `5219993437008` (WhatsApp)

Para agregar un usuario:
```bash
# Desde el panel admin (requiere token de admin)
POST /api/admin/users
{ "user_id": "nuevo@email.com", "display_name": "Nombre", "role": "user" }
```

### Rate Limiting

- **15 requests por 60 segundos** por usuario
- Almacenado en Redis con ventana deslizante
- HTTP 429 cuando se supera el límite

### Endpoints internos

Algunos endpoints solo son llamables desde dentro del cluster (k8s-agent, whatsapp-bridge, CronJobs):
```
Header: Authorization: Bearer <INTERNAL_API_SECRET>
```

---

## 9. Cómo Solicitar Cambios y Mejoras

Esta es la parte más importante del manual. Hay diferentes tipos de cambios y cada uno tiene su forma correcta de pedirlo.

---

### 9.1 Cambios simples de configuración

Son cambios que no requieren código nuevo. Ejemplos:
- Cambiar el modelo LLM
- Ajustar límites de rate limiting
- Cambiar URLs de servicios

**Cómo pedirlo:**
```
"Cambia el modelo LLM de qwen2.5:14b a llama3.1:8b"
"Aumenta el rate limit a 30 requests por minuto"
"Cambia el intervalo del loop SRE a 90 segundos"
```

Lo que se modifica: variables de entorno en `k8s/config/01-configmap.yaml` o `k8s/agents/05-backend-deployment.yaml`, sin cambio de código ni rebuild de imagen.

---

### 9.2 Nuevas capacidades de un agente existente

Agregar algo que un agente ya conoce pero no sabe hacer. Ejemplo:
- Que Camael pueda cancelar pipelines de CI
- Que Haniel pueda crear eventos recurrentes
- Que Raphael detecte un nuevo tipo de anomalía

**Cómo pedirlo:**
```
"Quiero que Camael pueda cancelar un workflow de GitHub Actions que esté
 corriendo. Debe poderse pedir con: 'cancela el pipeline actual'"

"Necesito que Raphael detecte cuando un pod lleva más de 10 minutos
 en estado Pending y notifique inmediatamente sin esperar el threshold"
```

**Qué se toca:** El archivo `agents/{nombre}/agent.py` del agente correspondiente, posiblemente una tool nueva en `tools/`.

---

### 9.3 Nuevo agente

Para una capacidad completamente nueva que ningún agente actual cubre.

**Cuándo pedir un nuevo agente vs. extender uno existente:**
- Nuevo agente: la capacidad es independiente, tiene su propia especialidad bien definida
- Extender existente: es una variante de lo que ya hace un agente

**Cómo pedirlo:**
```
"Necesito un agente que gestione las finanzas personales. Debe poder:
 1. Consultar gastos del mes desde un Google Sheet
 2. Categorizar gastos automáticamente
 3. Enviar resumen semanal por WhatsApp
 Llámalo Sachiel o el nombre que consideres apropiado."
```

**Qué se crea:**
- `agents/{nombre}/agent.py` con clase `XAgent(BaseAgent)`
- Registro en `agents/base/agent_registry.py`
- Posiblemente nuevas keywords en `orchestration/agent_router.py`

---

### 9.4 Nuevo endpoint REST

Cuando necesitas acceder a una funcionalidad específica desde afuera del chat.

**Cómo pedirlo:**
```
"Necesito un endpoint GET /api/sre/runbooks que devuelva la lista de
 todos los runbooks disponibles en Qdrant con su contenido resumido"

"Crea un endpoint POST /api/admin/broadcast que envíe un mensaje
 por WhatsApp a todos los usuarios activos"
```

**Qué se crea:** Archivo en `interfaces/api/routers/` + registro en `main.py`.

---

### 9.5 Nueva integración (tool o skill)

Cuando el sistema necesita hablar con un servicio externo nuevo.

**Diferencia entre Tool y Skill:**
- **Tool**: integración con servicio externo específico (Prometheus, GitHub, Slack...)
- **Skill**: capacidad reutilizable de infraestructura (LLM, RAG, K8s, Vault...)

**Cómo pedirlo:**
```
"Necesito integrar Slack. Los agentes deben poder enviar mensajes a canales
 específicos cuando detecten problemas críticos. El token está en la variable
 SLACK_BOT_TOKEN"

"Necesita una skill para interactuar con la API de Jira: crear tickets,
 buscar issues por proyecto, actualizar estados"
```

---

### 9.6 Cambios en el pipeline LangGraph

Modificar la secuencia de cómo se procesan las solicitudes generales.

**Cómo pedirlo:**
```
"Quiero que antes de que Sariel genere el plan, haya un paso de validación
 que verifique si la pregunta tiene contexto suficiente. Si no lo tiene,
 que pregunte al usuario antes de continuar"

"El Supervisor (Remiel) debe tener un umbral de calidad diferente para
 preguntas técnicas (mínimo 7) vs preguntas conversacionales (mínimo 5)"
```

---

### 9.7 Infraestructura Kubernetes

Para cambios en el cluster: nuevos servicios, ajuste de recursos, configuración de red.

**Cómo pedirlo:**
```
"Necesito desplegar un servicio de notificaciones push (Firebase FCM).
 Debe estar en el namespace amael-ia, puerto 8020, con 256Mi de memoria"

"Aumenta los límites de memoria del backend a 2Gi, el actual de 1Gi
 está causando OOMKilled ocasionalmente"

"Agrega un CronJob que limpie documentos en MinIO con más de 30 días
 sin acceso, que corra todos los domingos a las 3am"
```

---

### 9.8 Dashboard o métricas nuevas

Para agregar visibilidad en Grafana o nuevas métricas de Prometheus.

**Cómo pedirlo:**
```
"Quiero un panel en Grafana que muestre cuántas veces por hora se activa
 cada agente, desglosado por agente y usuario"

"Necesito una métrica que cuente cuántos documentos tiene indexados cada
 usuario en Qdrant, para poder alertar si alguien supera 1000 documentos"
```

---

### 9.9 Buenas prácticas para pedir cambios

**Sé específico sobre el comportamiento:**
```
❌ "Mejora el agente de código"
✅ "Cuando Jophiel genere código Python, debe incluir siempre type hints
    en las funciones y un docstring con el formato Google Style"
```

**Menciona el contexto de uso:**
```
❌ "Agrega autenticación por API key"
✅ "Necesito que el endpoint /api/devops/ci-hook también acepte autenticación
    por API key (header X-API-Key) para integrarlo con GitLab que no soporta
    webhooks HMAC"
```

**Indica restricciones importantes:**
```
✅ "El nuevo agente no debe hacer llamadas externas — todo debe procesarse
    localmente para cumplir con las políticas de privacidad de la empresa"

✅ "El CronJob debe correr solo de lunes a viernes y nunca durante el horario
    de mantenimiento (viernes 11pm - sábado 2am)"
```

**Prioriza claramente:**
```
✅ "Esto es urgente, hay un bug en producción: cuando el usuario envía un PDF
    mayor a 10MB el proceso se cuelga sin responder error. Necesito un límite
    de 5MB con mensaje de error claro."

✅ "Esto no es urgente, es una mejora de UX: cuando la respuesta tarde más de
    5 segundos, enviar un mensaje de 'procesando...' al usuario mientras espera"
```

---

### 9.10 Proceso de deploy de un cambio

Una vez que se implementa un cambio:

```
1. Código en develop (rama de trabajo)
         ↓
2. PR de develop → main (aprobación requerida)
         ↓
3. CI: Tests & Lint (automático, ~2 min)
         ↓
4. Aprobación del PR (tú das el voBo)
         ↓
5. Merge → main
         ↓
6. CI: Build & Push imagen (self-hosted, ~8 min)
         ↓
7. CI: Deploy a K8s + rollout status
         ↓
8. Raphael activa monitoreo intensificado 10 min post-deploy
         ↓
9. Si falla el deploy → notificación WhatsApp de Camael
```

**Versioning:** La versión de la imagen se lee del manifest `k8s/agents/05-backend-deployment.yaml`. Para pedir un deploy de una versión específica:
```
"Sube la versión del backend a 1.9.0 con los cambios actuales"
```

---

## 10. Referencia Rápida de Endpoints

### Chat

```
POST /api/chat
Body: { "prompt": "...", "conversation_id": "opcional" }
Auth: Bearer JWT
```

```
GET  /api/conversations           # listar conversaciones
POST /api/conversations           # crear conversación
GET  /api/conversations/{id}      # obtener mensajes
DELETE /api/conversations/{id}    # eliminar
```

### Documentos (RAG)

```
POST /api/ingest
Body: multipart/form-data, campo "file" (PDF/TXT/DOCX/MD)
→ Indexa en Qdrant para búsqueda semántica

GET /api/documents
→ Lista todos tus documentos indexados
```

### Memoria

```
GET /api/memory                   # listar episodios recordados
DELETE /api/memory/{memory_id}    # olvidar un episodio
DELETE /api/memory                # GDPR wipe — borrar toda tu memoria
```

### SRE

```
GET /api/sre/loop/status          # estado del loop autónomo
GET /api/sre/incidents            # últimos incidentes detectados
GET /api/sre/slo/status           # SLOs con burn rates en vivo
GET /api/sre/postmortems          # postmortems generados por LLM
GET /api/sre/maintenance          # ventana de mantenimiento activa
POST /api/sre/maintenance         # activar mantenimiento
DELETE /api/sre/maintenance       # desactivar mantenimiento
```

### Admin (requiere rol admin)

```
GET  /api/admin/users             # listar usuarios
POST /api/admin/users             # agregar usuario
PATCH /api/admin/users/{uid}      # cambiar rol/status
GET  /api/admin/settings          # configuración de la plataforma
PATCH /api/admin/settings         # actualizar configuración
```

### Perfil

```
GET  /api/profile                 # tu perfil (rol, timezone, preferencias)
PATCH /api/profile                # actualizar preferencias
```

### DevOps

```
POST /api/devops/ci-hook          # webhook GitHub (interno, firmado HMAC)
```

---

## 11. Troubleshooting Común

### "El bot no responde en WhatsApp"

```bash
# 1. Verificar que el bridge está corriendo
kubectl get pods -n amael-ia -l app=whatsapp-bridge

# 2. Ver logs del bridge
kubectl logs -n amael-ia deploy/whatsapp-bridge-deployment --tail=50

# 3. Si el bridge está en Error (Chromium lock):
kubectl run fix-lock --image=busybox --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"whatsapp-session-pvc"}}],"containers":[{"name":"fix","image":"busybox","command":["sh","-c","rm -f /data/WWebJS/Default/SingletonLock && echo done"],"volumeMounts":[{"name":"v","mountPath":"/data"}]}]}}' \
  -n amael-ia
kubectl delete pod fix-lock -n amael-ia
kubectl rollout restart deploy/whatsapp-bridge-deployment -n amael-ia
```

### "Las respuestas del LLM son muy lentas"

```bash
# Verificar que Ollama tiene la GPU
kubectl logs -n amael-ia deploy/ollama-deployment --tail=20 | grep -i gpu

# Si el pod de Ollama está Pending (con single GPU, usar delete en vez de restart):
kubectl delete pod -l app=ollama -n amael-ia
```

### "Un deploy falló, quiero volver a la versión anterior"

```bash
# Ver historial de deployments
kubectl rollout history deployment/amael-agentic-deployment -n amael-ia

# Rollback al deployment anterior
kubectl rollout undo deployment/amael-agentic-deployment -n amael-ia

# Rollback a una revisión específica
kubectl rollout undo deployment/amael-agentic-deployment -n amael-ia --to-revision=3
```

### "Vault está sellado (después de reinicio del nodo)"

```bash
kubectl port-forward -n vault svc/vault 8200:8200 &
export VAULT_ADDR="http://localhost:8200"
vault operator unseal <KEY_1>
vault operator unseal <KEY_2>
vault operator unseal <KEY_3>
# Las keys están en vault.root (NUNCA en git)
```

### "El CI/CD está fallando"

1. Ve a `https://github.com/ricardogs26/Amael-AgenticIA/actions`
2. Identifica qué job falló y el step específico
3. Dile a Claude Code: _"El CI está fallando en el step X del job Y, aquí está el log: [pega el error]"_

### "Necesito ver qué está procesando el backend en este momento"

```bash
kubectl logs -n amael-ia deploy/amael-agentic-deployment -f --tail=50 | \
  grep -E "request_id|intent|agent|ERROR"
```

### Comandos SRE por WhatsApp

Puedes controlar el SRE directamente desde WhatsApp:

| Comando | Qué hace |
|---------|----------|
| `/sre status` | Estado del loop, circuit breaker, SLOs |
| `/sre incidents` | Últimos 5 incidentes |
| `/sre postmortems` | Últimos 3 postmortems |
| `/sre maintenance on 60` | Activar mantenimiento por 60 minutos |
| `/sre maintenance off` | Desactivar mantenimiento |
| `/sre slo` | Ver objetivos de SLO |
| `/sre ayuda` | Lista de comandos |

---

## Glosario

| Término | Significado |
|---------|-------------|
| **LangGraph** | Framework de grafos para orquestar el pipeline Sariel→Executor→Remiel |
| **RAG** | Retrieval-Augmented Generation — busca en tus documentos antes de responder |
| **Qdrant** | Base de datos vectorial donde se guardan los documentos para búsqueda semántica |
| **Ollama** | Servidor local de LLMs — corre qwen2.5:14b en la GPU del servidor |
| **Vault** | HashiCorp Vault — guarda secretos cifrados (tokens de Google OAuth) |
| **MicroK8s** | Distribución ligera de Kubernetes corriendo en `lab-home` |
| **Cloudflare Tunnel** | Túnel seguro que expone los servicios internos a internet sin abrir puertos |
| **SRE** | Site Reliability Engineering — práctica de operar sistemas con confiabilidad |
| **SLO** | Service Level Objective — objetivo de disponibilidad (ej: 99.5% en 24h) |
| **OTel** | OpenTelemetry — estándar para trazas distribuidas y métricas |
| **ADR** | Architecture Decision Record — documento que registra una decisión técnica |
| **self-hosted runner** | El runner de CI/CD que corre dentro del cluster (pod `github-runner`) |

---

*Última actualización: 2026-03-17 — versión backend 1.8.1*
