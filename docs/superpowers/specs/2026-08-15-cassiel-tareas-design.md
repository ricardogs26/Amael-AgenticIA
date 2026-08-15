# Cassiel — Gestor de tareas pendientes (diseño)

**Fecha**: 2026-08-15 · **Estado**: aprobado por Ricardo (secciones 1–5 en conversación)
**Enfoque elegido**: A — extender Cassiel (`agents/scheduler/`), sin agente nuevo.

## Objetivo

Cassiel pasa de "recordatorios cron" a gestor de pendientes conversacional por
WhatsApp: captura tareas al vuelo, completa la ficha con máximo 1–2 preguntas,
mantiene lista priorizada (personal/laboral), insiste hasta que se marquen
hechas, agenda citas en Google Calendar, y un análisis nocturno alimenta el
brief de las 7:00.

Fuera de alcance: exportar a Obsidian (futuro), UI en frontend-next, contactar
a terceros para agendar (visión futura), integración directa con Structured
(gratis vía su sync con Google Calendar).

## 1. Modelo de datos

Tabla `user_tasks` en PostgreSQL (DDL en `agents/scheduler/storage.py`, junto a
`user_jobs`):

| Campo | Tipo | Notas |
|---|---|---|
| `id` | serial PK | |
| `user_id` | text | email canónico, como `user_jobs` |
| `title` | text | corto |
| `description` | text | opcional |
| `category` | text | `personal` \| `laboral` (CHECK) |
| `priority` | text | `alta` \| `media` \| `baja` (CHECK) |
| `estimated_minutes` | int null | |
| `due_date` | date null | null = sin fecha |
| `status` | text | `pending` \| `done` \| `cancelled` (CHECK) |
| `needs_scheduling` | bool default false | dispara flujo de agenda |
| `calendar_event_id` | text null | evento creado en Google Calendar |
| `last_nudge_at` | timestamptz null | control de insistencia |
| `created_at`, `completed_at` | timestamptz | |

Tabla `task_briefs`: `(id, user_id, brief_date date, analysis text,
created_at)` — resultado del análisis nocturno.

Reglas EN CÓDIGO (nunca en el prompt — qwen no compara ni valida):
- Enums validados contra CHECK + validación Python antes del INSERT.
- Tope 50 tareas `pending` por usuario.
- Orden efectivo determinista: vencida/hoy > alta > media > baja; desempate
  por `due_date` más próxima, luego `created_at`.

## 2. Captura

1. Router: ampliar patrones del intent `reminder` (que ya rutea a Cassiel) con
   frases de tarea: «tengo que», «necesito», «pendiente», «anota», «apunta».
   `reminder` sigue ANTES que `memory` (lección 1.14.0).
2. El LLM de Cassiel (mismo patrón: `format=json`, `reasoning=False`, solo
   TRADUCE) clasifica: recordatorio programado → flujo `user_jobs` actual;
   tarea → ficha: `title, category, priority, estimated_minutes, due_date?,
   needs_scheduling`.
3. Preguntas al capturar — decisión en código, máximo 1–2:
   - falta `due_date` Y `priority == alta` → preguntar fecha;
   - `needs_scheduling` → flujo de agenda (§5).
   Lo demás se infiere; se corrige conversando («esa es laboral»).
4. Confirmación en un mensaje: «Anotada 📝 <título> — <categoría>, prioridad
   <p>, ~<min> min. [¿pregunta?]».
5. Pregunta pendiente en Redis `task:pending_question:<user>` TTL 10 min: si
   el siguiente mensaje la responde, completa la ficha; si no, se descarta
   sin bloquear (el mensaje se procesa normal por el dispatcher).

## 3. Seguimiento e insistencia

- Recordatorio puntual: tick de 60 s existente (runner.py); tareas con
  `due_date == hoy` y sin nudge hoy → WhatsApp ~9:00.
- Escalado por prioridad, vencidas: `alta` nudge diario; `media` cada 3 días;
  `baja` solo brief. `last_nudge_at` = máx 1 nudge/tarea/día; tope global
  3 nudges/día por usuario (ganan las más prioritarias).
- Cierre: «ya lo hice / ya compré X» → match por similitud contra títulos
  `pending`; resolución EN CÓDIGO: 1 candidato claro → cerrar; 2+ → preguntar
  cuál; 0 → decirlo. `status=done`, `completed_at=now()`.
- Cancelar: «cancela la de X» → `cancelled`, mismo mecanismo de match.
- Posponer: «mejor el lunes» → actualizar `due_date`, silencia nudges hasta
  entonces.

## 4. Análisis nocturno + brief

- Job en el scheduler del backend, 03:45 México (tras el consolidador de
  03:30), LLM en tier CPU (`OLLAMA_DEEP_URL`) — la GPU no se toca.
- Lock Redis `SET NX` (el backend corre 2 réplicas — lección 1.15.2).
- Por usuario con tareas `pending`: análisis → top del día y por qué,
  estancadas >7 días, agrupables, y MÁXIMO una pregunta de información
  faltante. Se guarda en `task_briefs`.
- Brief 7:00 (day-planner): sección «Pendientes» = top-3 del día (orden
  determinista), vencidas con días de atraso, análisis/pregunta del nocturno.
  Sin tareas → la sección no aparece.
- Guarda: si el nocturno falló, el brief sale con la lista ordenada por
  código, sin análisis. El brief JAMÁS se bloquea por el LLM.
- `next_run_time` del job se loguea al arrancar (lección runbooks nivel 3).

## 5. Flujo de agenda

1. `needs_scheduling=true` → consultar Google Calendar vía productivity-service
   (OAuth/Vault existente); proponer huecos en la confirmación: reglas en
   código — L-V 9:00–18:00, bloques de `estimated_minutes` (default 60),
   próximos 7 días, máx 3 propuestas.
2. Respuesta («el martes 10») → crear evento en Calendar, guardar
   `calendar_event_id`, `due_date` = fecha del evento. Confirmación pendiente
   por el mismo Redis TTL de §2.
3. Agendar NO cierra la tarea — se cierra al confirmar que se hizo.
4. Solo escribe en el calendario del usuario. Nunca contacta terceros.
5. Si productivity-service falla (Vault sellado, token vencido), la tarea se
   guarda igual sin huecos y se avisa («no pude leer tu agenda»).

## Comandos nuevos (bridge + Cassiel)

| Comando | Acción |
|---|---|
| `/pendientes` | lista priorizada completa |
| `/pendientes laboral` · `/pendientes personal` | filtrada |
| `/pendientes hoy` | solo vencidas + para hoy |
| conversacional | crear, cerrar, cancelar, posponer, repriorizar |

Al implementar: agregar los comandos al menú de `/ayuda` del bridge
(ConfigMap `whatsapp-bridge-code`).

## Errores y observabilidad

- Métricas: `amael_tasks_total{status}`, `amael_task_nudges_total`,
  `amael_task_brief_runs_total{result}`.
- El tick nunca truena por una tarea: excepción por tarea → log + continuar.
- LLM caído en captura → «anótala así: …» pidiendo el mínimo, o fallback a
  guardar título crudo con defaults (media/personal) — capturar nunca falla.

## Pruebas

- Unit: orden determinista de prioridad, escalado de nudges (tope diario),
  match de cierre (1/2+/0 candidatos), validación de enums, tope 50.
- Unit: parser de ficha con respuestas JSON malformadas del LLM.
- Integración: flujo captura→pregunta→respuesta vía Redis TTL; nocturno con
  lock (no corre doble); brief con y sin análisis.
- E2E manual: los tres ejemplos de Ricardo (contrato renta, café, dentista).

## Fases de entrega

1. **Fase 1**: tabla + captura + `/pendientes` + cierre/cancelar/posponer +
   recordatorio puntual + nudges. (Utilizable desde aquí.)
2. **Fase 2**: análisis nocturno + sección en brief + `/ayuda` actualizado.
3. **Fase 3**: flujo de agenda con Calendar.
