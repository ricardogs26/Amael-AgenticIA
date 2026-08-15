"""
Cassiel — scheduler conversacional.

Angelología: Cassiel (קפציאל) es el ángel del tiempo y la paciencia.

Convierte lenguaje natural («mándame el resumen del trader todos los días a las
3pm») en filas de `user_jobs`. La división de responsabilidades es la de trader
1.0.31: el LLM SOLO traduce la intención a un JSON con acción y schedule; todo
lo que se puede validar en código —cron parseable, fecha futura, tope de jobs,
a quién pertenece el job— se valida en código. Si el schedule no parsea, el
error legible vuelve al usuario para que reformule; Cassiel no inventa.

El user_id viene del AgentContext/task armado por el dispatcher a partir del
JWT — NUNCA del JSON del modelo: un LLM que pudiera nombrar al dueño del job
podría crear o borrar recordatorios de otro usuario.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.cassiel")

_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL           = os.environ.get("LLM_MODEL", "qwen3:14b")
_LLM_TIMEOUT_S   = int(os.environ.get("SCHEDULER_LLM_TIMEOUT_S", "60"))

_SYSTEM = """Eres Cassiel, el planificador de tareas de Amael. Traduce la petición del usuario a UN objeto JSON, sin texto adicional.

Esquema:
{
  "action": "create" | "list" | "pause" | "resume" | "delete" | "unclear" | "task_create" | "task_list" | "task_done" | "task_cancel" | "task_postpone",
  "title": "nombre corto de la tarea (solo create)",
  "prompt": "instrucción que se ejecutará en cada corrida, autocontenida (solo create)",
  "schedule": "cron de 5 campos O timestamp ISO local (solo create)",
  "delivery": "whatsapp" | "none",
  "job_ref": "id o parte del título de la tarea (pause/resume/delete)",
  "clarification": "pregunta al usuario (solo unclear)",
  "task": {                        // solo task_create
    "title": "corto", "description": "",
    "category": "personal" | "laboral",
    "priority": "alta" | "media" | "baja",
    "estimated_minutes": 30,
    "due_date": "YYYY-MM-DD" | null,
    "needs_scheduling": false
  },
  "task_ref": "id o parte del título",   // done/cancel/postpone
  "new_due": "YYYY-MM-DD",               // solo task_postpone
  "filter": "personal" | "laboral" | "hoy" | null   // task_list
}

Reglas:
- "schedule" en la zona horaria del usuario (se te indica). Recurrente → cron de 5 campos. Una sola vez ("mañana a las 9") → timestamp ISO.
- "prompt" debe ser ejecutable sin contexto de esta conversación: "resumen del estado del trader", no "eso que te pedí".
- Si el usuario pregunta qué tareas tiene → action=list.
- Si falta el cuándo o el qué → action=unclear con una clarification concreta.
- NUNCA inventes fechas: si el usuario no dio hora, pregunta.
- delivery=whatsapp por default.
- Tarea pendiente SIN horario recurrente («tengo que», «necesito», «anota»,
  «recuérdame X» sin cuándo) → task_create. Con cron/hora explícita → create (job).
- Infiere category/priority/estimated_minutes con sentido común; due_date SOLO
  si el usuario dio fecha — no la inventes.
- «ya lo hice / ya compré X» → task_done con task_ref. «cancela» → task_cancel.
  «mejor el lunes» → task_postpone con new_due.
- «/pendientes» o «qué tengo pendiente» → task_list (filter si lo dijo)."""


@AgentRegistry.register
class CassielAgent(BaseAgent):
    """Cassiel — recordatorios y tareas programadas por chat."""

    name         = "cassiel"
    role         = "Scheduler conversacional — recordatorios y tareas programadas"
    version      = "1.0.0"
    capabilities = ["job_create", "job_list", "job_pause", "job_resume", "job_delete"]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        query      = (task.get("query") or "").strip()
        user_email = (task.get("user_email") or self.context.user_id or "").strip()

        if not user_email:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="user_email requerido")
        if not query:
            return AgentResult(success=False, output=None, agent_name=self.name,
                               error="query vacío")

        from agents.scheduler import storage

        tz_name = storage.user_timezone(user_email)
        pending = self._pop_pending_question(user_email)
        extra_context = None
        if pending:
            extra_context = (
                f"Pregunta pendiente al usuario: fecha para la tarea "
                f"#{pending.get('task_id')}. Si este mensaje la responde, "
                f"action=task_postpone con task_ref={pending.get('task_id')} y new_due."
            )
        try:
            parsed = self._parse_intent(query, tz_name, extra_context)
        except Exception as exc:
            logger.warning(f"[cassiel] parseo LLM falló: {exc}")
            return AgentResult(
                success=True, agent_name=self.name,
                output={"response": (
                    "No pude interpretar la tarea. Dime qué quieres que haga y "
                    "cuándo — por ejemplo: «mándame el resumen del trader todos "
                    "los días a las 3pm»."
                )},
            )

        try:
            answer = self._apply(parsed, user_email, tz_name)
        except ValueError as exc:
            # Errores de validación en código (cron malo, tope, ambigüedad):
            # el mensaje ya es legible y es la respuesta correcta al usuario.
            answer = str(exc)

        return AgentResult(success=True, agent_name=self.name, output={"response": answer})

    # ── LLM: intención → JSON ─────────────────────────────────────────────────

    def _parse_intent(self, query: str, tz_name: str, extra_context: str | None = None) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_ollama import ChatOllama

        ahora = datetime.now(ZoneInfo(tz_name))
        llm = ChatOllama(
            model=_MODEL,
            base_url=_OLLAMA_BASE_URL,
            temperature=0.1,
            reasoning=False,          # qwen3: sin thinking dentro del contenido
            format="json",            # Ollama fuerza JSON válido en runtime
            client_kwargs={"timeout": _LLM_TIMEOUT_S},
        )
        contexto = (
            f"Ahora es {ahora.strftime('%A %Y-%m-%d %H:%M')} en {tz_name}.\n"
            f"Petición del usuario: {query}"
        )
        if extra_context:
            contexto = f"{contexto}\n{extra_context}"
        resp = llm.invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=contexto),
        ])
        data = json.loads(resp.content if isinstance(resp.content, str)
                          else str(resp.content))
        if not isinstance(data, dict):
            raise ValueError(f"El LLM no devolvió un objeto: {type(data)}")
        return data

    # ── Código: JSON validado → acción ────────────────────────────────────────

    def _apply(self, parsed: dict, user_email: str, tz_name: str) -> str:
        from agents.scheduler import storage

        action = str(parsed.get("action", "")).lower().strip()

        if action == "unclear":
            return str(parsed.get("clarification")
                       or "¿Qué quieres que programe y cuándo?")

        if action == "create":
            title    = str(parsed.get("title") or "").strip()
            prompt   = str(parsed.get("prompt") or "").strip()
            schedule = str(parsed.get("schedule") or "").strip()
            delivery = str(parsed.get("delivery") or "whatsapp").strip().lower()
            if not (title and prompt and schedule):
                return ("Me falta el qué o el cuándo. Ejemplo: «recuérdame revisar "
                        "el PR todos los lunes a las 9am».")
            job = storage.create_job(
                user_id=user_email, title=title, prompt=prompt,
                schedule=schedule, tz_name=tz_name, delivery=delivery,
            )
            pub = storage.to_public(job)
            tipo = "una sola vez" if job.one_shot else f"recurrente ({job.schedule})"
            entrega = "por WhatsApp" if job.delivery == "whatsapp" else "sin entrega"
            return (f"✅ Tarea #{job.id} creada: *{job.title}* — {tipo}, {entrega}. "
                    f"Próxima ejecución: {pub['next_run']}.")

        if action == "list":
            jobs = storage.list_jobs(user_email)
            if not jobs:
                return "No tienes tareas programadas."
            lineas = []
            for j in jobs:
                pub    = storage.to_public(j)
                estado = f"próxima {pub['next_run']}" if j.enabled else "⏸ pausada"
                lineas.append(f"#{j.id} *{j.title}* — {j.schedule} — {estado} "
                              f"({j.run_count} corridas)")
            return "Tus tareas programadas:\n" + "\n".join(lineas)

        if action in ("pause", "resume", "delete"):
            ref = str(parsed.get("job_ref") or "").strip()
            if not ref:
                return "¿Cuál tarea? Dame su número o parte del nombre."
            job = storage.find_job(user_email, ref)
            if not job:
                return f"No encontré ninguna tarea que coincida con {ref!r}."
            if action == "delete":
                storage.delete_job(job.id, user_email)
                return f"🗑 Tarea #{job.id} *{job.title}* eliminada."
            storage.set_enabled(job.id, user_email, enabled=(action == "resume"))
            if action == "pause":
                return f"⏸ Tarea #{job.id} *{job.title}* pausada."
            pub = storage.to_public(storage.find_job(user_email, str(job.id)))
            return (f"▶️ Tarea #{job.id} *{job.title}* reanudada. "
                    f"Próxima ejecución: {pub['next_run']}.")

        if action in ("task_create", "task_list", "task_done", "task_cancel",
                      "task_postpone"):
            return self._apply_task(action, parsed, user_email, tz_name)

        return f"Acción desconocida del parser: {action!r}. Reformula la petición."

    def _apply_task(self, action: str, parsed: dict, user_email: str, tz_name: str) -> str:
        from agents.scheduler import tasks_storage

        try:
            if action == "task_create":
                t = parsed.get("task") or {}
                titulo = str(t.get("title") or "").strip()
                if not titulo:
                    return "¿Qué tarea anoto? Dímela en una frase corta."
                due = None
                if t.get("due_date"):
                    due = date.fromisoformat(str(t["due_date"]))   # ValueError → legible
                task = tasks_storage.create_task(
                    user_id=user_email, title=titulo,
                    description=str(t.get("description") or ""),
                    category=str(t.get("category") or "personal"),
                    priority=str(t.get("priority") or "media"),
                    estimated_minutes=(int(t["estimated_minutes"])
                                       if t.get("estimated_minutes") else None),
                    due_date=due,
                    needs_scheduling=bool(t.get("needs_scheduling")),
                )
                extra = ""
                if due is None and task.priority == "alta":
                    self._set_pending_question(user_email, task.id, "due_date")
                    extra = " ¿Para cuándo la necesitas?"
                mins = f", ~{task.estimated_minutes} min" if task.estimated_minutes else ""
                return (f"📝 Anotada #{task.id}: *{task.title}* — {task.category}, "
                        f"prioridad {task.priority}{mins}."
                        f"{f' Fecha: {due}.' if due else ''}{extra}")

            if action == "task_list":
                tareas = tasks_storage.list_pending(user_email)
                filtro = str(parsed.get("filter") or "").lower() or None
                hoy = datetime.now(ZoneInfo(tz_name)).date()
                if filtro in ("personal", "laboral"):
                    tareas = [t for t in tareas if t.category == filtro]
                elif filtro == "hoy":
                    tareas = [t for t in tareas
                              if t.due_date is not None and t.due_date <= hoy]
                if not tareas:
                    return "No tienes pendientes. 🎉" if not filtro else \
                           f"Sin pendientes con filtro {filtro!r}."
                lineas = []
                for t in tasks_storage.sorted_pending(tareas, hoy):
                    marca = ("🔴" if t.due_date and t.due_date < hoy else
                             "🟡" if t.due_date == hoy else "•")
                    fecha = f" — para {t.due_date}" if t.due_date else ""
                    mins = f" (~{t.estimated_minutes}m)" if t.estimated_minutes else ""
                    lineas.append(f"{marca} #{t.id} {t.title}{mins}{fecha}")
                return "Tus pendientes:\n" + "\n".join(lineas)

            # task_done / task_cancel / task_postpone
            ref = str(parsed.get("task_ref") or "").strip()
            if not ref:
                return "¿Cuál pendiente? Dame su número o parte del nombre."
            task = tasks_storage.find_task(user_email, ref)   # ValueError si 2+
            if not task:
                return f"No encontré ningún pendiente que coincida con {ref!r}."
            if action == "task_done":
                tasks_storage.set_status(task.id, user_email, "done")
                return f"✅ Cerrada #{task.id}: *{task.title}*."
            if action == "task_cancel":
                tasks_storage.set_status(task.id, user_email, "cancelled")
                return f"🗑 Cancelada #{task.id}: *{task.title}*."
            nueva = date.fromisoformat(str(parsed.get("new_due") or ""))
            tasks_storage.postpone_task(task.id, user_email, nueva)
            return f"⏭ #{task.id} *{task.title}* pospuesta al {nueva}."
        except ValueError as exc:
            # Errores de validación en código (categoría inválida, ambigüedad,
            # fecha inválida): el mensaje ya es legible.
            return str(exc)

    _PENDING_TTL_S = 600

    def _set_pending_question(self, user: str, task_id: int, field: str) -> None:
        try:
            from storage.redis.client import get_redis_client
            get_redis_client().setex(
                f"task:pending_question:{user}", self._PENDING_TTL_S,
                json.dumps({"task_id": task_id, "field": field}),
            )
        except Exception as exc:
            logger.debug(f"[cassiel] pending_question no guardada: {exc}")

    def _pop_pending_question(self, user: str) -> dict | None:
        try:
            from storage.redis.client import get_redis_client
            raw = get_redis_client().getdel(f"task:pending_question:{user}")
            return json.loads(raw) if raw else None
        except Exception:
            return None
