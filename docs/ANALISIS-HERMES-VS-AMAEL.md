# Análisis comparativo — Hermes Agent vs Amael-IA

**Fecha**: 7-ago-2026
**Fuente**: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT — su código se puede adaptar libremente)
**Complementa**: `PLAN-BUCLE-APRENDIZAJE.md` (fases 1–6, enfocado al bucle de aprendizaje).
Este documento cubre los **seis pilares completos** y qué tomar de cada uno.

El estado de Amael citado aquí está **verificado en código y en el cluster**
(6-ago-2026), no en READMEs — que en este repo mienten (ver
`project_zaphkiel_readmes_obsoletos` y la nota de corrección del plan).

---

## Resumen ejecutivo

| Pilar Hermes | Amael hoy | Veredicto |
|---|---|---|
| 1. Memoria + auto-mejora | Zaphkiel reparado el 6-ago (estuvo caído desde su despliegue); sin destilación ni bloque estable | **Adoptar patrón** — Fases 1–2 del plan |
| 2. Skills procedurales | Clases Python fijas; runbooks solo SRE (1317 pts, consolidador que no alcanza) | **Adoptar formato SKILL.md** — Fases 3–4 |
| 3. Gateway multiplataforma | WhatsApp ×2 | **Descartar el gateway; tomar 2 ideas puntuales** (ledger de entrega, DM pairing) |
| 4. MCP | Nada | **Adoptar al final** — Fase 6 |
| 5. Cron | CronJobs K8s + APScheduler — completo en infra, **inmanejable desde el chat** | **Nueva fase: cron conversacional** — el hueco más subestimado |
| 6. Runtime flexible | MicroK8s | **Descartar**; la sub-idea útil (subagentes) ya existe como handoff |

---

## Pilar 1 — Memoria y auto-mejora

### Lo que Hermes hace

- `MEMORY.md` (~2 200 chars) + `USER.md` (~1 375 chars) en disco, **inyectados
  completos como snapshot congelado** al inicio de cada sesión.
- El agente los **escribe él mismo** al detectar preferencias, convenciones,
  correcciones — con *nudges* periódicos que le recuerdan hacerlo. Gate de
  aprobación opcional (`write_approval`, apagado por default).
- Búsqueda de sesiones con **FTS5 sobre SQLite, mensajes crudos**: ~20 ms,
  0 tokens, sin resumen del LLM ni truncado.
- Honcho y 7 proveedores externos *complementan* la memoria propia, nunca la
  reemplazan.

### Lo que Amael tiene (verificado)

- **Zaphkiel** (`agents/memory_agent/agent.py`): store/retrieve/forget/list
  sobre Qdrant `memory_{user_id}`. Cableado en `chat.py` — pero **estuvo caído
  en silencio desde su despliegue** hasta el fix de `1.13.0` (AgentContext
  incompleto tragado por un `except`). La colección se creó el 6-ago: en la
  práctica la memoria está vacía y la Fase 1 no tiene qué destilar todavía.
- `pg_trgm` + índice GIN sobre `messages.content` y `GET /api/conversations?search=`
  — existe, pero **solo el front lo usa**; el LLM no puede buscar su historial.
- `user_profile` con campos fijos escritos por endpoints, no por el agente.

### Qué tomar

1. **El patrón, no el código**: hechos destilados inyectados **completos** al
   system prompt (Fase 2 del plan). La lección central de Hermes es que la
   memoria de más valor no se recupera por coseno — se inyecta siempre. Sus
   topes (~2.2k/1.4k chars) son un punto de partida razonable; en la ruta
   rápida (qwen3:1.7b, 4 096 tokens, prompt medido ~330) el tope se aplica en
   código, nunca pidiéndole al LLM que resuma para caber.
2. **Los nudges**: Hermes le recuerda al agente escribir memoria
   periódicamente. Equivalente barato en Amael: un hook en `after_execute` o
   al cierre de conversación que evalúe si el turno dejó algo digno de
   `store`. Hoy el store es incondicional por turno con umbral heurístico
   (0.3) — medir la distribución antes de tocarlo.
3. **Session search como herramienta del LLM** (Fase 5): mismo principio que
   su FTS5 — devolver mensajes crudos, sin resumen, `user_id` forzado desde
   `AgentContext`. Nuestro backend es `pg_trgm`, ya indexado y en producción.

**No tomar**: Honcho (dependencia externa para modelar al usuario — Postgres +
destilación propia cubre el caso), ni `write_approval: false` por default.

---

## Pilar 2 — Skills como memoria procedural

### Lo que Hermes hace

- Formato **`SKILL.md`**: frontmatter YAML (nombre, descripción, versión) +
  secciones *Cuándo usar / Procedimiento / Peligros comunes / Verificación* +
  archivos de soporte. Compatible con el estándar abierto agentskills.io.
- **Progressive disclosure**: al prompt solo llegan nombre + descripción de una
  línea; el cuerpo se carga cuando la skill se elige.
- **`skill_manage`**: el agente crea/parcha/edita/borra sus propias skills.
  `patch` preferido sobre `edit` por eficiencia. Gate de aprobación opcional.
- Skills = comandos slash; hub con escaneo de seguridad para skills de terceros.

### Lo que Amael tiene (verificado)

- `skills/` = clases Python (`SkillRegistry`): kubernetes, rag, vault, llm,
  git, web, api_call, filesystem. **El agente no puede escribir una.**
- La memoria procedural real vive en `sre_runbooks` (1 317 puntos) con un bucle
  de 3 niveles validado — pero solo para Raphael, y con una fuga medida el
  6-ago: `HIGH_RESTARTS` acumula **625 auto-generados con 1 consolidado**,
  porque el consolidador procesa `MAX_RUNBOOKS_PER_TYPE=10` una vez al día.
- Sin progressive disclosure: los prompts de agentes llevan sus capacidades
  completas.

### Qué tomar

1. **El formato `SKILL.md` tal cual** — es un estándar, y sus cuatro secciones
   (*Cuándo usar / Procedimiento / Peligros / Verificación*) son exactamente la
   estructura que los runbooks consolidados ya siguen a medias. Fase 3 del
   plan: colección Qdrant `agent_skills`, `owner_agent` + `scope`, empezando
   por `scope="sre"`.
2. **`skill_manage` con el gate SIEMPRE encendido**, reutilizando la
   aprobación por WhatsApp de Camael (`agents/sre/approvals.py`). La acción
   `patch` (falla si el texto no aparece exacto) es mejor diseño que reescribir:
   copiarla.
3. **Progressive disclosure** (Fase 4): `list_skills_brief()` +
   herramienta `skill_load(name)`. En una sola RTX 5070 con qwen3:14b, el
   contexto es el recurso escaso — aquí vale más que en Hermes.

**Pendiente propio que este pilar expone** (no es de Hermes, lo midió el
grafo): decidir qué hacer con el consolidador que no alcanza — subir el tope
por tipo, correrlo más seguido, o purgar por antigüedad. Decisión de tuning con
datos, previa a generalizar el mecanismo.

---

## Pilar 3 — Gateway multiplataforma

### Lo que Hermes hace

- Un proceso con **adaptadores por plataforma** (Telegram, Discord, Slack,
  WhatsApp, Signal, Matrix, email, SMS…), session store por chat, matriz de
  capacidades (voz, hilos, reacciones, streaming).
- **Allowlist + DM pairing**: usuario desconocido recibe un código de
  emparejamiento válido 1 h en vez de ser editado a mano en una config.
- **Delivery ledger**: registro durable que **reentrega** la respuesta si el
  gateway muere entre la generación y la confirmación de envío.

### Lo que Amael tiene

- `whatsapp-bridge` + `whatsapp-personal`. Identity check por mensaje contra el
  backend, `convMap{}` **en memoria**, código servido por ConfigMap.

### Qué tomar

**El gateway completo, no** — ya descartado: WhatsApp cubre el caso real y cada
plataforma extra es superficie de mantenimiento. Pero dos ideas puntuales valen:

1. **Ledger de entrega en el bridge**. Hoy, si el bridge muere entre la
   respuesta del backend y el `client.sendMessage`, el mensaje se pierde sin
   rastro (convMap y cola viven en memoria). Un ledger mínimo en Redis
   (`pending_delivery:{id}` con TTL, marcado al confirmar) + reintento al
   arrancar replica la garantía de Hermes con ~50 líneas en `index.js`.
   Relevante porque el bridge usa `Recreate` y se reinicia completo en cada
   deploy.
2. **DM pairing**: un código de 1 h generado por el admin vía `/sre` o el chat,
   en vez de editar `K8S_ALLOWED_USERS_CSV` y redeployar. Baja fricción para
   dar de alta a alguien (la boda de abril lo habría usado).

---

## Pilar 4 — MCP

### Hermes
Cliente MCP con **filtrado de herramientas por servidor** y soporte de sampling.
Cualquier servidor MCP = herramientas nuevas sin código nativo.

### Amael
Cero referencias a MCP en el código.

### Qué tomar
La Fase 6 del plan, sin cambios: cliente MCP montado sobre `tools/registry.py`,
con el filtrado por servidor como requisito (un MCP de terceros no debe exponer
sus 40 tools al prompt — mismo argumento de contexto escaso del Pilar 2). Va al
final: amplía el catálogo pero no aporta al bucle de aprendizaje. Candidatos
concretos cuando llegue: MCP de Grafana y de Kubernetes de terceros, para
comparar contra las tools propias.

---

## Pilar 5 — Cron y automatización ⚠️ el hueco subestimado

### Lo que Hermes hace

- Herramienta **`cronjob`** que el propio agente usa: lenguaje natural
  ("every morning at 9am"), delays relativos, cron clásico, ISO timestamps.
- Jobs en JSON plano con escritura atómica; tick de 60 s; sesión de agente
  fresca por job; **skills adjuntas** que se inyectan antes del prompt.
- Entrega componible: chat de origen, plataforma específica, "all".
- Ciclo de vida completo desde el chat: `/cron` pause/resume/run/edit/remove.
- **No-agent mode**: scripts programados que entregan stdout sin LLM.
- **Pre-run gates**: el script previo puede decidir `wakeAgent: false` y
  ahorrarse la invocación del LLM.
- **Job chaining** (`context_from`): la salida de un job alimenta al siguiente.

### Lo que Amael tiene

- **Infraestructura completa**: CronJobs de K8s (day-planner, watchdog,
  night-watch), APScheduler (loop SRE 60 s, consolidador 03:00, trader 10 min).
  El watchdog *es* el "no-agent mode" de Hermes; sus pre-run gates son
  conceptualmente el dedup de Redis.
- **Cero capacidad conversacional**: no existe forma de que un usuario diga por
  WhatsApp «recuérdame revisar el PR cada lunes a las 9» o «mándame el resumen
  del trader cada cierre de mercado». Todo cron nuevo = editar YAML + kubectl.

### Qué tomar

**Una herramienta `scheduler` para los agentes** — la brecha real de este pilar
no es de infraestructura sino de interfaz:

- Tabla `user_jobs` en Postgres (equivalente a su `jobs.json`, pero en el store
  que ya hay): prompt, schedule, user_id, delivery, next_run_at, enabled.
- APScheduler ya corre en el backend — un tick que lea la tabla es el mismo
  patrón del loop SRE.
- Ejecución: el prompt del job entra por `AgentDispatcher` como si fuera un
  mensaje del usuario (sesión fresca, igual que Hermes) y la respuesta sale por
  `POST /send` del bridge — ambos existen.
- Registrada como tool, el LLM puede crear/pausar/listar jobs desde el chat.
  El parseo de lenguaje natural a schedule lo hace el propio LLM al llamar la
  tool (que reciba cron o ISO ya validado en código — misma lección de trader
  1.0.31: lo comparable en código no se delega al modelo).

Esto no estaba en el plan original y es probablemente **el pilar con mejor
relación valor/esfuerzo después de la memoria**: convierte capacidades que ya
existen (dispatcher, bridge, APScheduler) en una feature de usuario.

---

## Pilar 6 — Runtime flexible

### Hermes
7 backends de ejecución (local, Docker, SSH, Modal, Daytona, Singularity,
Vercel Sandbox), instalación en un VPS de $5, Termux en Android.

### Amael
MicroK8s de un nodo con GPU dedicada. **No hay problema que resolver** —
descartado, igual que en el plan.

### Sub-ideas del pilar que sí valen mención

- **`delegate_task` (subagentes paralelos, 3 concurrentes, contexto aislado,
  solo el resumen vuelve al padre)**: Amael ya tiene el 80% — el batch executor
  paraleliza pasos y los handoffs (Raphael→Camael) son delegación real con
  contexto propio. Lo que no hay es delegación *ad-hoc* decidida por el LLM
  fuera del plan. No lo priorizaría: el Planner→Grouper ya cumple ese rol de
  forma más controlada.
- **`execute_code` (Python que llama tools vía RPC en sandbox)**: potente y
  peligroso. En un cluster con Vault y un trader con dinero real, un sandbox de
  ejecución de código generado necesitaría un aislamiento que hoy no existe.
  No adoptar.
- **Context files** (`.hermes.md`, `AGENTS.md`, `CLAUDE.md`…): ya es la
  práctica de este repo.

---

## Qué se puede copiar literalmente (MIT)

| De Hermes | Para | Adaptación |
|---|---|---|
| Formato `SKILL.md` + parser de frontmatter | Fase 3 | Directa — es un estándar |
| Semántica de `skill_manage` (patch > edit, staging con approval) | Fase 3 | Reimplementar sobre Qdrant + WhatsApp approval |
| Esquema de job del cron (`jobs.json`) | Scheduler conversacional | Traducir a DDL de Postgres |
| Topes de memoria (2.2k / 1.4k chars) y snapshot congelado | Fase 2 | Números de partida, tope en código |
| Delivery ledger del gateway | whatsapp-bridge | Redis en vez de disco |

Lo que **no** conviene portar: su stack (SQLite, filesystem local, uv) — Amael
ya tiene Postgres/Redis/Qdrant para cada uno de esos roles, y duplicar stores
es cómo se pudren los datos.

---

## Roadmap consolidado

Las fases 1–6 del `PLAN-BUCLE-APRENDIZAJE.md` siguen vigentes. Este análisis
agrega tres elementos y fija el orden completo:

| # | Elemento | Pilar | Origen | Prereq |
|---|---|---|---|---|
| 1 | Destilación de memoria (consolidador → Zaphkiel) | 1 | Plan F1 | dejar juntar episodios (~1 semana desde 6-ago) |
| 2 | Bloque de perfil estable en el prompt | 1 | Plan F2 | #1 |
| 3 | **Scheduler conversacional** (`user_jobs` + tool) | 5 | **nuevo** | — |
| 4 | Session search como tool del LLM | 1 | Plan F5 | — |
| 5 | Skills `SKILL.md` + `skill_manage` con gate | 2 | Plan F3 | decidir tuning del consolidador |
| 6 | Progressive disclosure | 2 | Plan F4 | #5 |
| 7 | **Delivery ledger del bridge** | 3 | **nuevo** | — |
| 8 | DM pairing | 3 | **nuevo** (opcional) | — |
| 9 | Cliente MCP con filtrado | 4 | Plan F6 | — |

Los ítems 3, 4 y 7 son independientes de todo y se pueden intercalar; el 3 es
el de mayor valor visible para el usuario final.

---

## Plan por fases y esfuerzos

Esfuerzo en días efectivos de trabajo (sesiones de este laboratorio: código +
tests + build + deploy + verificación en cluster). Los riesgos altos llevan su
mitigación pegada.

### Etapa A — independientes, valor inmediato (~4–6 días)

| Fase | Entregable | Esfuerzo | Riesgo | Versión |
|---|---|---|---|---|
| **A1. Scheduler conversacional** | Tabla `user_jobs`, tick APScheduler, tool `scheduler` (crear/listar/pausar/borrar desde chat y WhatsApp), entrega por `POST /send` | **2–3 d** | Medio — un job mal escrito puede spamear WhatsApp; mitigar con tope de jobs por usuario y dedup tipo watchdog | 1.14.0 |
| **A2. Session search como tool** | Tool sobre el SQL `pg_trgm` ya indexado; `user_id` forzado desde `AgentContext`, mensajes crudos sin resumen | **0.5–1 d** | Bajo — la query ya corre en prod | 1.13.x |
| **A3. Delivery ledger del bridge** | Redis `pending_delivery:{id}` + reintento al arrancar; solo ConfigMap `whatsapp-bridge-code`, sin rebuild de imagen | **0.5–1 d** | Bajo — cuidar no duplicar mensajes (marcar antes de enviar, confirmar después) | bridge CM |
| **A4. DM pairing** *(opcional)* | Código de 1 h en Redis, alta en `user_identities` al canjearlo | **1 d** | Medio — es superficie de acceso; solo el admin genera códigos | 1.14.x |

Validación A1: crear un job por WhatsApp («resumen del trader a las 15:00»),
verlo llegar, pausarlo desde el chat. A3: matar el pod del bridge entre
generación y envío y ver el mensaje reentregado al arrancar.

### Etapa B — memoria (prerreq: ~1 semana juntando episodios desde el 6-ago) (~2–3 días)

| Fase | Entregable | Esfuerzo | Riesgo | Versión |
|---|---|---|---|---|
| **B1. Destilación** | `agents/memory_agent/consolidator.py` (modelado sobre el de runbooks), job 03:30 tier profundo, hechos `kind="fact"` | **1–2 d** | Bajo — aditivo; la trampa conocida es agendar el `add_job` de verdad (el nivel 3 de runbooks nació sin agendar) | 1.15.0 |
| **B2. Bloque de perfil en el prompt** | `render_profile_block()` con tope duro en código (~600–900 tokens), caché Redis TTL 1 h invalidado al escribir | **1 d** | Medio — toca la ruta caliente (470 ms medidos); medir latencia antes/después, si sube >100 ms va solo en la ruta completa | 1.15.1 |

Antes de B1: medir la distribución real de `importance` en `memory_*` — el
umbral 0.3 no se toca por intuición.

Validación B2: preguntar algo sin relación léxica con una preferencia guardada
(«respuestas cortas» + pregunta sobre Kong) y ver que la respeta.

### Etapa C — skills procedurales (~4–6 días)

| Fase | Entregable | Esfuerzo | Riesgo | Versión |
|---|---|---|---|---|
| **C0. Tuning del consolidador** | Decisión con datos sobre los 625 `HIGH_RESTARTS` sin consolidar: subir tope por tipo, correr más seguido, o purgar por edad | **0.5 d** | Bajo — es medición + un cambio de config | raphael 1.1.x |
| **C1. SKILL.md + `skill_manage`** | Colección `agent_skills`, parser de frontmatter, acciones create/patch/delete, **gate de aprobación por WhatsApp encendido siempre** (flujo de Camael), arranque con `scope="sre"` | **3–4 d** | **Alto** — superficie nueva de auto-escritura en una plataforma con trader de dinero real; el gate no es negociable y el scope acotado tampoco | 1.16.0 |
| **C2. Progressive disclosure** | `list_skills_brief()` + tool `skill_load(name)`; prompts con una línea por skill | **1 d** | Medio — puede degradar la elección de skill; comparar tasa de acierto en un set fijo de 20 peticiones antes de dar por buena | 1.16.1 |

Validación C1: Raphael resuelve una anomalía nueva → propone skill → llega por
WhatsApp → se aprueba → la siguiente ocurrencia la recupera de `agent_skills`.

### Etapa D — expansión (~2–3 días)

| Fase | Entregable | Esfuerzo | Riesgo | Versión |
|---|---|---|---|---|
| **D1. Cliente MCP** | Cliente sobre `tools/registry.py` con filtrado de tools por servidor (requisito, no opción) | **2–3 d** | Medio — cada servidor conectado es superficie; empezar con uno de solo lectura (Grafana) | 1.17.0 |

### Totales y secuencia

```
Etapa A  ██████ 4–6 d   (independiente — se puede empezar hoy)
Etapa B  ███    2–3 d   (bloqueada ~1 semana por acumulación de episodios)
Etapa C  ██████ 4–6 d   (C0 primero; C1 es el único riesgo alto del plan)
Etapa D  ███    2–3 d   (última a propósito: no alimenta el bucle)
─────────────────────
Total    12–18 días efectivos
```

Orden recomendado: **A1 → A2 → A3 → (B espera datos) → B1 → B2 → C0 → C1 → C2 → D1**,
con A4 intercalable cuando haga falta dar de alta a alguien. La Etapa A se hace
mientras B espera — el calendario natural es empezar A hoy y B cae sola en una
semana.
