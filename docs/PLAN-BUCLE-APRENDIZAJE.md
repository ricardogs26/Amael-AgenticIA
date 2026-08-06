# Plan — Bucle de aprendizaje (inspirado en Hermes Agent)

**Fecha**: 6-ago-2026
**Referencia**: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT)
**Objetivo**: cerrar el bucle *usar → destilar → reutilizar → corregir* fuera del dominio SRE.

---

## 1. Estado real de Amael (verificado en código, no en READMEs)

Los READMEs de `memory/` y `agents/memory_agent/` dicen "Placeholder — no implementado".
**Están obsoletos.** El código dice otra cosa:

| Capa (diagrama del propio README de `memory/`) | Implementación real | Estado |
|---|---|---|
| Working memory | `AgentState` (TypedDict) | ✅ |
| Semantic (documentos) | `agents/researcher/rag_retriever.py` — Qdrant por usuario | ✅ |
| **Episodic** | **`agents/memory_agent/agent.py` — ZaphkielAgent, 388 líneas** | ✅ **implementado y cableado** |
| Procedural | `skills/` — clases Python | ⚠️ el agente **no las escribe** |

### Lo que Zaphkiel ya hace

- `store`: cada turno del chat, fire-and-forget desde `interfaces/api/routers/chat.py:621`
- Puerta por importancia heurística sin LLM (`_compute_importance`, umbral `0.3`)
- `retrieve`: k=4 inyectado al contexto del chat (`chat.py:608`)
- `forget` / GDPR wipe + `list` → router `interfaces/api/routers/memory.py`
- Colección `memory_{user_id}` en Qdrant, 768 dims

### Lo que también existe y no había visto

- `messages` tiene extensión `pg_trgm` + índice GIN sobre `content` (`main.py:392`)
- `GET /api/conversations?search=` ya busca con `ILIKE` sobre títulos y mensajes

### Corrección (6-ago-2026, al instrumentar el grafo)

Zaphkiel estaba **cableado pero muerto**. Los tres call sites construían
`AgentContext` con solo `request_id` y `user_id`; el dataclass exige además
`conversation_id` y `llm`, así que lanzaban `TypeError`. En `chat.py` el
`except Exception → logger.debug` lo hacía invisible; en `/api/memory` salía
como 503. Reparado en `1.13.0` con test de regresión
(`tests/unit/agents/test_memory_context_contract.py`).

**Consecuencia para este plan**: la Fase 1 no tiene nada que destilar todavía.
Las colecciones `memory_{user_id}` de Qdrant están vacías o casi. Hay que dejar
correr la memoria unos días con el fix desplegado antes de medir la
distribución de `importance` y antes de que la consolidación tenga insumo.

### Los huecos reales

1. **Sin destilación.** Zaphkiel guarda episodios crudos y los recupera por similitud
   coseno. Nunca consolida 30 episodios en un hecho, nunca detecta contradicción,
   nunca olvida por obsolescencia. Es el equivalente a tener los runbooks de nivel 1
   sin el consolidador de nivel 3.
2. **Sin bloque estable de perfil.** Hermes inyecta `MEMORY.md` + `USER.md` **completos**
   en cada sesión. Amael solo inyecta lo que el coseno recupera: si la pregunta no se
   parece léxicamente a "prefiero respuestas cortas", esa preferencia no llega al prompt.
3. **Memoria procedural encerrada en SRE.** `runbook_consolidator.py` es un bucle de
   aprendizaje real y validado (11 tipos, 155 incidentes → 1 runbook), pero solo lo
   consume Raphael y la colección está clavada a `sre_runbooks`.
4. **La búsqueda de historial no es una herramienta del agente.** Existe el índice y
   existe el endpoint, pero solo el front los usa. Amael no puede contestar "¿qué te dije
   del trader la semana pasada?".
5. **Sin progressive disclosure.** `SkillRegistry.list_skills()` existe pero el prompt no
   se arma con descripciones de una línea.
6. **Sin cliente MCP.** Cero referencias en el código.

---

## 2. Los 6 pilares de Hermes contra Amael

| Pilar Hermes | Mecanismo | Amael | Acción |
|---|---|---|---|
| Memoria + auto-mejora | `MEMORY.md`/`USER.md` inyectados completos + nudges + FTS5 | Zaphkiel (episodios) | **Fases 1–2** |
| Skills = memoria procedural | `SKILL.md` + `skill_manage` (create/patch) + progressive disclosure | Runbooks solo SRE | **Fases 3–4** |
| Gateway multiplataforma | 15–20 plataformas | WhatsApp ×2 | Descartado — cubre el caso |
| MCP | Cliente con filtrado de tools | — | **Fase 6** |
| Cron | Tareas en lenguaje natural | day-planner, watchdog, trader, consolidador | ✅ completo |
| Runtime flexible | 7 backends | MicroK8s | Descartado — no aplica |

---

## Fase 1 — Destilación de memoria (Zaphkiel nivel 2)

**Tesis**: el consolidador de runbooks ya resuelve esto para SRE. Se generaliza.

**Qué**: cronjob nocturno que lee los episodios de un usuario, los agrupa por
`episode_type`, y sintetiza con LLM del tier profundo un conjunto pequeño de *hechos
destilados* (`kind="fact"`, con `supersedes` apuntando a los episodios que resume).

**Archivos**:
- `agents/memory_agent/consolidator.py` — nuevo, modelado sobre `agents/sre/runbook_consolidator.py`
- `agents/memory_agent/agent.py` — acción `consolidate`; `_retrieve` prioriza `kind="fact"`
- `main.py` lifespan — `add_job` a las 03:30 hora de México (03:00 ya la ocupa el consolidador SRE)

**Gotcha heredado**: el nivel 3 de runbooks estuvo **roto desde su creación** porque
ningún `add_job` lo agendaba pese a que la doc decía que corría. La validación de esta
fase no es "el código existe" — es un log del job con conteo de hechos generados.

**Tier**: `get_chat_llm(tier="deep")` → `ollama-cpu`. Trabajo nocturno pesado, no debe
desalojar el modelo interactivo de la VRAM (mismo criterio que raphael 1.1.13).

**Antes de tocar el umbral `0.3`**: medir. Query sobre las colecciones `memory_*` con
distribución de `importance` y cuántos episodios se descartan. Nada de subir o bajar el
umbral por intuición.

**Validación**: `GET /api/memory` de un usuario con >100 episodios devuelve ≤15 hechos
destilados y la suma de episodios crudos baja.

**Riesgo**: bajo. Aditivo, el `retrieve` actual sigue funcionando si la consolidación falla.

---

## Fase 2 — Bloque de perfil estable en el prompt

**Tesis**: la lección más fuerte de Hermes. El coseno no sirve para preferencias — recupera
fragmentos sueltos y se le escapa lo que no coincide léxicamente. Los hechos estables
se inyectan **completos**, siempre.

**Qué**: los hechos destilados de la Fase 1 se renderizan como un bloque de texto plano
(~600–900 tokens máx) que se antepone al system prompt de cada conversación.

**Archivos**:
- `agents/memory_agent/profile.py` — `render_profile_block(user_id) -> str`, con tope duro de caracteres
- `orchestration/fast_chat.py` — el `_SYSTEM_PROMPT` pasa a ser plantilla + bloque
- `interfaces/api/routers/chat.py` — mismo bloque en la ruta completa
- Caché en Redis, TTL 1 h, invalidado al escribir memoria

**Restricción dura**: la ruta rápida usa `qwen3:1.7b` con **4096 tokens de contexto**, y el
prompt medido hoy es de ~330 tokens con 10 mensajes de historial. Un bloque de 900 tokens
es el 22% de la ventana. El tope se aplica en código (truncar por prioridad de hecho),
**nunca se le pide al LLM que resuma para caber** — misma lección que trader 1.0.31 y 1.0.4:
lo que se puede calcular en código no se delega al modelo.

**Validación**: preguntar algo sin relación léxica con una preferencia guardada y verificar
que la respuesta la respeta. Ejemplo: preferencia "respuestas cortas" + pregunta sobre Kong.

**Riesgo**: medio. Toca el prompt de la ruta caliente (470 ms medidos). Medir latencia antes
y después; si sube >100 ms, el bloque va solo en la ruta completa.

---

## Fase 3 — Skills procedurales escribibles por el agente

**Tesis**: es el pilar con más valor y el que no existe en ninguna forma general.
Hoy `skills/` son clases Python — el agente no puede crear una.

**Qué**: segundo tipo de skill, en markdown, con el formato `SKILL.md` de Hermes
(frontmatter YAML + secciones *Cuándo usar / Procedimiento / Peligros / Verificación*).
Se guardan en Qdrant, colección `agent_skills`, con `owner_agent` y `scope`.

**Archivos**:
- `skills/procedural/store.py` — CRUD sobre Qdrant + parseo del frontmatter
- `skills/procedural/manage.py` — herramienta `skill_manage` con acciones `create`/`patch`/`delete`
- `agents/sre/runbook_consolidator.py` — parametrizar `_SRE_RUNBOOKS_COLLECTION` y la
  agrupación por campo, para que sirva a `agent_skills` sin duplicar la lógica
- `tools/registry.py` — registrar `skill_manage`

**Puerta de aprobación — no negociable.** Hermes deja `write_approval: false` por default.
Aquí va **encendido desde el día uno**, reutilizando el flujo de aprobación por WhatsApp
que Camael ya usa para los PRs (`agents/sre/approvals.py`). Un agente que se auto-modifica
sin revisión, en una plataforma donde el trader opera dinero real, no es algo que se quiera
depurar después de los hechos.

**Validación**: pedirle a Raphael que resuelva una anomalía nueva, verificar que propone
una skill, aprobarla por WhatsApp, y que la siguiente ocurrencia la recupere.

**Riesgo**: alto. Es superficie nueva de escritura. Empezar con `scope="sre"` (dominio ya
probado) antes de abrirlo a los demás agentes.

---

## Fase 4 — Progressive disclosure de skills

**Qué**: el system prompt recibe solo `nombre — descripción de una línea` por skill.
El cuerpo se carga con una herramienta `skill_load(name)` cuando el agente la elige.

**Archivos**:
- `skills/registry.py` — `list_skills_brief()` (nombre + primera línea del docstring)
- `skills/procedural/manage.py` — herramienta `skill_load`
- Los `_SYSTEM_PROMPT` de los agentes que hoy listan capacidades completas

**Por qué importa aquí más que en Hermes**: una sola RTX 5070 y un modelo de 14b. El
contexto es el recurso escaso del laboratorio, no el dinero de la API.

**Validación**: medir tokens de entrada del prompt del ejecutor antes y después
(`LLM_TOKENS_TOTAL{token_type="input"}` ya existe).

**Riesgo**: bajo, pero puede degradar la elección de skill si las descripciones son malas.
Comparar tasa de acierto sobre un set fijo de 20 peticiones antes de dar por buena la fase.

---

## Fase 5 — Búsqueda de sesiones como herramienta del agente

**Qué**: exponer la búsqueda que ya existe (`pg_trgm` + GIN sobre `messages.content`) como
herramienta invocable por el LLM, no solo como endpoint del front.

**Archivos**:
- `tools/session_search/` — nueva tool, con `user_id` forzado desde el contexto
- `interfaces/api/routers/conversations.py` — extraer el SQL a una función reutilizable

**Detalle**: Hermes devuelve **mensajes crudos, sin resumen del LLM y sin truncar**
(~20 ms, 0 tokens de coste). Hacer lo mismo: nada de resumir con LLM lo que se puede
devolver tal cual.

**Seguridad**: el `user_id` viene del `AgentContext`, jamás de un parámetro que el modelo
pueda escribir. Sin esto, la herramienta es una fuga de conversaciones entre usuarios.

**Riesgo**: bajo. La consulta ya está indexada y probada en producción.

---

## Fase 6 — Cliente MCP

**Qué**: cliente MCP con filtrado de herramientas por servidor, montado sobre el
`tools/registry.py` existente.

**Riesgo**: bajo, valor incremental. **Va al final a propósito**: amplía el catálogo de
herramientas pero no aporta nada al bucle de aprendizaje, que es el objeto de este plan.

---

## 3. Orden y dependencias

```
Fase 1 (destilación) ──► Fase 2 (bloque de perfil)      [dependencia dura]
Fase 3 (skills md)   ──► Fase 4 (progressive disclosure) [dependencia dura]
Fase 5 (session search)   independiente
Fase 6 (MCP)              independiente
```

Las fases 1–2 y 3–4 son dos carriles independientes entre sí. La 5 se puede intercalar
en cualquier momento — es la más barata de todas.

**Recomendación de arranque**: Fase 1. Reutiliza un módulo ya validado en producción,
es aditiva, y sin ella la Fase 2 no tiene qué inyectar.

---

## 4. Lo que se descarta explícitamente

| Pilar Hermes | Por qué no |
|---|---|
| 7 runtimes (Docker/SSH/Modal/Daytona/…) | MicroK8s de un nodo. No hay problema que resolver. |
| Gateway de 15–20 plataformas | WhatsApp ×2 cubre el caso de uso real. Telegram es ~1 día si algún día se quiere. |
| `write_approval: false` por default | Ver Fase 3. En este ecosistema la puerta va encendida. |
| Skills Hub / agentskills.io | Instalar skills de terceros en un cluster con Vault y credenciales de broker no compensa. |

---

## 5. Versiones previstas

| Fase | Versión backend | Manifest |
|---|---|---|
| 1 | `1.13.0` | `k8s/agents/05-backend-deployment.yaml` |
| 2 | `1.13.1` | idem |
| 3 | `1.14.0` | idem + ConfigMap de aprobaciones |
| 4 | `1.14.1` | idem |
| 5 | `1.13.2` | idem (independiente) |
| 6 | `1.15.0` | idem |
