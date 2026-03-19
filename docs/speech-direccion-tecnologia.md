# Presentación para Dirección de Tecnología
## Plataforma Amael-AgenticIA — Agentes Autónomos de Software

**Duración estimada:** 15–20 minutos
**Audiencia:** Dirección de Tecnología
**Tono:** Ejecutivo con sustento técnico
**Versión en producción:** `amael-agentic-backend:1.8.7` / `k8s-agent:1.6.7`

---

## Puntos de énfasis para Dirección

> Leer antes de entrar a la sala. Estos son los mensajes que deben quedar grabados.

- **No es un chatbot.** Es una plataforma de agentes especializados que actúan sin intervención humana, coordinados por un orquestador central.
- **Autónomo pero supervisado.** Cada acción pasa por guardrails, circuit breakers y control de acceso por rol. El sistema no actúa a ciegas.
- **El cluster se repara solo.** Raphael (SRE Agent) detecta fallos, los diagnostica con un modelo de lenguaje y ejecuta acciones correctivas, incluyendo rollback automático si el fix falla.
- **CI/CD completamente automatizado.** Un merge a main desencadena tests, build, push al registry privado, despliegue a Kubernetes y verificación post-deploy — sin intervención humana.
- **Todo corre en casa.** LLM local (qwen2.5:14b en GPU RTX 5070), registry privado, secrets cifrados. No hay dependencia de APIs de terceros para el LLM.
- **Extensible por diseño.** Agregar un nuevo agente es agregar un módulo Python y registrarlo. El orquestador lo descubre automáticamente.

---

## Sección 1 — Apertura: El problema que resuelve

**Tiempo estimado: 2 minutos**

---

El software moderno genera una cantidad de operaciones repetitivas que consumen tiempo de ingeniería de alto valor: revisar el estado del cluster, diagnosticar un pod caído, crear una rama, escribir el código de un fix, abrir un pull request, coordinar un despliegue, preparar el plan del día.

La respuesta convencional es contratar más personas o comprar más herramientas. Nosotros tomamos un camino diferente: construir agentes de software especializados que ejecutan esas operaciones de forma autónoma.

**Amael-AgenticIA no es un chatbot con mejor interfaz.** Es una plataforma donde cada agente tiene un nombre, un rol definido, capacidades específicas y la autonomía para actuar — dentro de límites controlados.

La plataforma lleva el nombre de Amael, el orquestador central. Los demás agentes son una familia de ángeles, cada uno con su especialidad.

*Nota del presentador: hacer una pausa breve aquí. El nombre "familia de ángeles" suele generar curiosidad — no explicar de más, dejar que la pregunta llegue sola.*

---

## Sección 2 — Los agentes: quiénes son y qué hacen

**Tiempo estimado: 4 minutos**

---

### Amael — El orquestador

Amael es el cerebro central. Cuando llega una tarea — ya sea por el frontend web, por WhatsApp o por API — Amael la recibe, entiende la intención y decide qué agentes intervienen y en qué orden.

No ejecuta él mismo. Coordina. Usa LangGraph para construir un grafo de ejecución dinámico: los pasos se planifican, se agrupan para ejecución en paralelo donde es posible, se ejecutan y se evalúan.

Amael también maneja la seguridad: autenticación JWT, rate limiting por usuario (15 requests por minuto), validación de prompts y sanitización de salidas.

---

### Sariel — El planificador

Cuando Amael recibe una tarea compleja, delega la planificación a Sariel.

Sariel descompone la tarea en pasos ejecutables concretos: qué tipo de acción es cada paso (búsqueda RAG, llamada a herramienta, razonamiento, generación de código), en qué orden deben ejecutarse y cuáles pueden ir en paralelo. El plan está limitado a 8 pasos máximo para mantener la trazabilidad.

*Nota del presentador: si preguntan "¿cómo sabe qué pasos dar?" — Sariel usa el modelo de lenguaje local (qwen2.5:14b) con un prompt estructurado que fuerza salida JSON. El plan se valida antes de ejecutarse.*

---

### Remiel — El supervisor de calidad

Al terminar la ejecución, Remiel recibe el resultado y lo evalúa con una puntuación de 0 a 10.

Si la puntuación es menor a 6, Remiel emite una señal de REPLAN: Sariel vuelve a planificar con el contexto adicional de por qué falló el primer intento. Este ciclo ocurre una sola vez para evitar loops infinitos.

Si la puntuación es 6 o más, Remiel acepta el resultado y el sistema responde al usuario.

---

### Gabriel — El agente de desarrollo

Gabriel es el agente que escribe código y opera el repositorio de forma autónoma.

Dado un ticket o una descripción de tarea, Gabriel:

1. Lee los archivos relevantes del repositorio via GitHub API
2. Genera el código del fix o la funcionalidad
3. Crea la rama correspondiente
4. Hace el commit con el código
5. Abre el pull request hacia `develop`
6. Notifica al equipo por WhatsApp con el link del PR

Todo esto sin intervención humana. El flujo de revisión y aprobación del PR sigue siendo humano — Gabriel llega hasta abrir el PR, no hasta mergear.

---

### Raphael — El SRE autónomo

Raphael merece su propia sección y la tendrá en breve. En términos simples: Raphael observa el cluster Kubernetes cada 60 segundos, detecta anomalías, las diagnostica y actúa.

---

### Sandalphon — El investigador

Sandalphon realiza búsquedas sobre los documentos del usuario (PDFs, textos, documentación interna indexada en base vectorial) y búsquedas en la web cuando el contexto interno no es suficiente.

Implementa RAG (Retrieval-Augmented Generation): recupera los fragmentos más relevantes para una consulta y los usa como contexto para la respuesta del LLM.

---

### Haniel — El agente de productividad

Haniel tiene acceso a Google Calendar y Gmail via OAuth. Puede consultar eventos del día, crear citas, redactar correos y generar resúmenes de agenda.

Los tokens OAuth se almacenan cifrados en Vault (HashiCorp). Haniel nunca maneja credenciales en texto plano.

---

### Uriel y Raziel — Arquitecto y CTO

Uriel es el agente de arquitectura: revisa decisiones de diseño, evalúa trade-offs técnicos, genera documentación de arquitectura.

Raziel es el agente CTO: síntesis de estado del sistema, métricas de negocio, reportes ejecutivos.

*Nota del presentador: Uriel y Raziel están en el registro de agentes pero su desarrollo activo está en roadmap. Mencionarlos como parte de la visión extensible de la plataforma.*

---

## Sección 3 — Flujo de una tarea: demo conceptual

**Tiempo estimado: 3 minutos**

---

Ejemplo real que podemos ejecutar en vivo:

> "Gabriel, en el repositorio amael-ia, el texto de la description se desborda en las tarjetas. Crea la rama fix/overflow desde develop, aplica el fix de CSS y abre un PR."

### Lo que ocurre internamente:

```
1. Usuario envía el mensaje (WhatsApp o frontend)
        ↓
2. Amael recibe, autentica JWT, valida el prompt
        ↓
3. Amael detecta intención: CODE_GENERATION → routing directo a Gabriel
        ↓
4. Sariel planifica los pasos:
   [RAG_RETRIEVAL] → buscar archivos CSS relevantes en el repo
   [CODE_GENERATION] → leer archivo → crear rama → generar fix → commit → PR
        ↓
5. Gabriel ejecuta:
   - Lee los archivos CSS actuales del repositorio via GitHub API
   - Genera el fix (overflow: hidden / text-overflow: ellipsis)
   - Crea rama fix/overflow desde develop
   - Hace commit con el código
   - Abre PR con descripción automática
        ↓
6. Remiel evalúa el resultado (puntuación: 8/10)
        ↓
7. Amael responde al usuario:
   "PR #47 abierto: fix/overflow → develop. Link: github.com/..."
        ↓
8. Notificación WhatsApp con el link del PR
```

**Tiempo total:** aproximadamente 45–90 segundos.

*Nota del presentador: si hay conectividad en la sala, esto se puede hacer en vivo. Si no, tener captura de pantalla del resultado de una ejecución anterior.*

---

## Sección 4 — Raphael: El SRE autónomo

**Tiempo estimado: 3 minutos**
**Sección de énfasis especial**

---

Raphael implementa el ciclo completo de Site Reliability Engineering de forma autónoma:

```
Observe → Detect → Diagnose → Decide → Act → Report
```

Este ciclo corre cada 60 segundos, ininterrumpidamente.

### Lo que Raphael observa

- Estado de todos los pods y nodos del cluster Kubernetes
- Métricas de CPU, memoria y tasa de errores HTTP desde Prometheus
- Tendencias: predicción lineal de agotamiento de disco, detección de memory leaks por derivada
- Presupuesto de error SLO: si `/api/chat` baja del 99.5% de disponibilidad en 24h, Raphael lo detecta y alerta

### Lo que Raphael detecta

| Anomalía | Origen |
|----------|--------|
| CrashLoop, OOM Kill, ImagePullError | Estado K8s |
| Pod pending, node not ready | Estado K8s |
| Alta CPU, alta memoria, alta tasa de errores | Prometheus |
| Predicción de disco lleno | predict_linear |
| Memory leak en tendencia | Derivada de métricas |
| Quema acelerada de presupuesto SLO | Prometheus |

### Cómo diagnostica

Raphael no usa reglas fijas. Consulta el modelo de lenguaje local con el contexto de la anomalía y los runbooks indexados en base vectorial (Qdrant). El diagnóstico tiene un confidence score de 0 a 1.

El diagnóstico histórico también influye: si el mismo tipo de problema tuvo éxito con ROLLOUT_RESTART el 80% de las veces en las últimas semanas, ese historial pondera el confidence score actual.

### Cómo actúa — con guardrails

Raphael tiene dos acciones posibles:

- **ROLLOUT_RESTART**: reinicia el deployment afectado automáticamente
- **NOTIFY_HUMAN**: notifica por WhatsApp y registra el incidente

Las acciones automáticas tienen guardrails:
- Máximo 3 reinicios por deployment en 15 minutos (circuit breaker)
- Solo actúa si el confidence score supera el umbral configurado (0.7 por defecto)
- Nunca actúa durante ventanas de mantenimiento activas

### Auto-rollback

Si Raphael ejecuta un ROLLOUT_RESTART y 5 minutos después el pod sigue sin levantar, y además detecta que hubo un deploy reciente (últimos 30 minutos), ejecuta un `kubectl rollout undo` automático.

Si no había deploy reciente, escala al humano: "El restart no resolvió el problema. Intervención manual requerida."

### Postmortems automáticos

Cuando un incidente se resuelve (por Raphael o por el humano), Raphael genera un postmortem en lenguaje natural usando el LLM: causa raíz, impacto, acción tomada, aprendizaje. Disponible via WhatsApp con `/sre postmortems`.

### Comandos WhatsApp

```
/sre status          → Estado del loop, circuit breaker, ventana de mantenimiento
/sre incidents       → Últimos 5 incidentes con severidad y acción
/sre postmortems     → Últimos 3 postmortems generados por LLM
/sre slo             → Estado de presupuesto de error por endpoint
/sre maintenance on 30  → Activar ventana de mantenimiento por 30 minutos
/sre maintenance off → Desactivar ventana de mantenimiento
```

*Nota del presentador: demostrar `/sre status` en vivo desde el WhatsApp. La respuesta llega en menos de 3 segundos.*

---

## Sección 5 — CI/CD Automatizado

**Tiempo estimado: 2 minutos**
**Sección de énfasis especial**

---

El pipeline de entrega está completamente automatizado. El disparador es un merge a la rama `main`.

### El flujo completo

```
1. Developer (o Gabriel) abre PR hacia develop
        ↓
2. GitHub Actions ejecuta Tests & Lint (obligatorio para poder mergear a main)
        ↓
3. PR develop → main requiere aprobación humana + status check verde
        ↓
4. Merge a main dispara el pipeline de producción:
   - Tests
   - docker build
   - docker push registry.richardx.dev (registry privado, en el cluster)
   - kubectl apply (actualiza el deployment en K8s)
   - kubectl rollout status (espera que el pod levante)
        ↓
5. Pipeline notifica a Raphael via POST /api/sre/deploy-hook
        ↓
6. Raphael activa monitoreo intensificado por 10 minutos en el deployment afectado
        ↓
7. Notificación WhatsApp: "Deploy amael-agentic-backend:1.8.7 completado. Raphael monitoreando."
```

El runner de GitHub Actions corre como un pod self-hosted dentro del mismo cluster. No hay dependencia de runners externos.

El versionado sigue los manifests de Kubernetes como fuente de verdad. El pipeline lee la versión directamente del YAML antes de buildear — no hay desincronización posible entre el manifest y la imagen deployada.

*Nota del presentador: enfatizar el punto de control humano: el PR develop→main requiere aprobación. La automatización no bypasea el voBo humano, lo complementa eliminando todo lo que viene después de ese voBo.*

---

## Sección 6 — Infraestructura: todo en casa

**Tiempo estimado: 2 minutos**

---

### El cluster

MicroK8s corriendo en un servidor single-node (lab-home). La decisión de single-node es deliberada para esta fase — simplicidad operativa, sin overhead de coordinación entre nodos. La arquitectura está lista para escalar a multi-nodo cuando sea necesario.

### El modelo de lenguaje

qwen2.5:14b corriendo en Ollama sobre una GPU NVIDIA RTX 5070 dedicada. Todos los agentes consumen este modelo vía API interna. No hay llamadas a OpenAI, Anthropic ni ninguna API externa de LLM.

Ventajas concretas:
- Latencia: ~2–5 segundos por generación (en GPU)
- Costo: $0 por token (el hardware ya está pagado)
- Privacidad: ningún dato sale del servidor

### Stack de infraestructura

| Componente | Función |
|-----------|---------|
| Kong | API Gateway — routing, rate limiting, autenticación |
| Vault | Gestión de secrets — tokens OAuth cifrados |
| PostgreSQL | Historial de conversaciones, incidentes SRE, aprendizaje |
| Redis | Cache de sesiones, deduplicación de alertas, ventanas de mantenimiento |
| Qdrant | Base vectorial — RAG por usuario, runbooks de Raphael |
| MinIO | Object storage para documentos subidos |
| Prometheus + Grafana | 8 dashboards de observabilidad — LLM, pipeline, SRE, seguridad, service map |
| Tempo | Trazabilidad distribuida de requests entre agentes |
| cert-manager | TLS automático via Cloudflare DNS challenge |

### Registry privado

Las imágenes de todos los servicios se almacenan en `registry.richardx.dev`, un registry privado self-hosted. No hay dependencia de Docker Hub ni de ningún registry externo.

---

## Sección 7 — Escenarios en vivo

**Tiempo estimado: 2 minutos**

---

Tres escenarios que podemos ejecutar en la reunión:

### Escenario A — Gabriel hace un fix de código

Desde WhatsApp o el frontend, enviar:

> "Gabriel, en el repositorio amael-ia, el título del modal está cortado en móvil. Crea la rama fix/modal-title desde develop, aplica el fix y abre un PR."

Observar en tiempo real: el PR aparece en GitHub con el código generado, la rama creada y la descripción automática. Tiempo esperado: 60–90 segundos.

### Escenario B — Raphael en acción

Desde WhatsApp:
```
/sre status
```

La respuesta muestra: estado del loop (running/paused), circuit breaker (open/closed), ventana de mantenimiento (activa/inactiva), cantidad de SLOs monitoreados.

Opcionalmente: escalar un pod a 0 réplicas manualmente y observar cómo Raphael detecta el problema en el siguiente ciclo (máximo 60 segundos) y notifica o actúa.

### Escenario C — Pipeline CI/CD

Hacer un commit pequeño en `develop` (un cambio en un comentario o una versión de patch), abrir el PR a `main`, aprobarlo y observar el pipeline ejecutarse completo en GitHub Actions: tests → build → push → deploy → notificación WhatsApp.

*Nota del presentador: el Escenario C requiere conectividad y unos 3–5 minutos. Si el tiempo es limitado, mostrar el historial de runs anteriores en GitHub Actions para ilustrar el flujo.*

---

## Sección 8 — Cierre: la visión

**Tiempo estimado: 1 minuto**

---

Lo que hemos construido es una plataforma, no una herramienta.

Cada agente nuevo que se agrega amplía las capacidades del sistema sin tocar el orquestador. El patrón está establecido: un módulo Python con su lógica, registrado en el AgentRegistry, y Amael lo descubre automáticamente.

La autonomía tiene límites deliberados. Raphael no puede borrar datos. Gabriel no puede mergear a producción sin aprobación humana. Los guardrails no son restricciones temporales — son parte del diseño.

El objetivo a mediano plazo es que las operaciones rutinarias de desarrollo y SRE sean cero-touch: el agente correcto, en el momento correcto, con la acción correcta — y el equipo humano enfocado en las decisiones que realmente requieren criterio.

---

## Preguntas frecuentes anticipadas

---

### "¿Qué pasa si el agente toma una acción incorrecta?"

Raphael tiene tres mecanismos de contención:

1. **Confidence threshold**: no actúa si el diagnóstico tiene menos del 70% de confianza.
2. **Circuit breaker**: después de 3 reinicios en 15 minutos, el circuit breaker se abre y Raphael solo notifica — no actúa — hasta que se restablezca manualmente.
3. **Verificación post-acción**: 5 minutos después de un restart, Raphael verifica si el pod está saludable. Si no lo está, escala al humano o ejecuta rollback automático.

El peor caso es un rollback automático a la versión anterior — no una acción destructiva.

---

### "¿El modelo de lenguaje puede inventar código incorrecto?"

Gabriel no publica código directamente a producción. Abre un PR. El código pasa por el proceso de revisión humana antes de llegar a `main`, y los tests automatizados deben pasar antes de que el PR pueda mergearse.

El flujo es: Gabriel propone, el humano aprueba, el pipeline verifica.

---

### "¿Qué tan seguro es tener un agente con acceso al cluster?"

Raphael opera con RBAC de Kubernetes configurado con el principio de mínimo privilegio:

- **ClusterRole `sre-agent-observer`**: solo lectura en pods, nodos, eventos y deployments en todos los namespaces.
- **Role `sre-agent-healer`**: solo en el namespace `amael-ia`, solo puede hacer restart de pods y crear Leases de liderazgo.

Raphael no puede acceder a secrets, modificar RBAC ni operar en namespaces de infraestructura crítica.

---

### "¿Cuánto cuesta operar esto?"

El costo operativo es el del servidor (hardware ya existente) más el dominio y el túnel Cloudflare. No hay costos por tokens de LLM ni por APIs externas.

El costo de desarrollo es el tiempo de ingeniería invertido en construir la plataforma.

---

### "¿Puede integrarse con nuestros sistemas existentes?"

Sí. La arquitectura de herramientas es extensible: agregar integración con un sistema externo (JIRA, Slack, PagerDuty, sistemas internos) implica implementar una `BaseTool` y registrarla en el `ToolRegistry`. El orquestador la descubre automáticamente.

Las APIs del backend están documentadas y autenticadas con JWT — cualquier sistema interno puede interactuar con los agentes via HTTP.

---

### "¿Qué pasa si el modelo de lenguaje local no es suficientemente capaz?"

qwen2.5:14b tiene rendimiento comparable a GPT-3.5 en tareas de razonamiento estructurado y generación de código. Para casos que requieran mayor capacidad, la arquitectura permite cambiar el modelo (hay un LLM adapter que puede apuntar a cualquier API compatible con OpenAI) sin modificar los agentes.

El cambio de modelo es una variable de entorno, no un cambio de código.

---

### "¿Esto está en producción o es un prototipo?"

Está en producción. Raphael ha estado corriendo el loop autónomo de forma continua. Gabriel ha abierto PRs reales en el repositorio. El pipeline CI/CD ha ejecutado decenas de deploys sin intervención manual.

La versión actual en producción es `amael-agentic-backend:1.8.7`.

---

*Documento generado el 2026-03-18. Versión para reunión con Dirección de Tecnología.*
