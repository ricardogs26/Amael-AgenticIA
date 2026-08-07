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
from datetime import datetime
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
  "action": "create" | "list" | "pause" | "resume" | "delete" | "unclear",
  "title": "nombre corto de la tarea (solo create)",
  "prompt": "instrucción que se ejecutará en cada corrida, autocontenida (solo create)",
  "schedule": "cron de 5 campos O timestamp ISO local (solo create)",
  "delivery": "whatsapp" | "none",
  "job_ref": "id o parte del título de la tarea (pause/resume/delete)",
  "clarification": "pregunta al usuario (solo unclear)"
}

Reglas:
- "schedule" en la zona horaria del usuario (se te indica). Recurrente → cron de 5 campos. Una sola vez ("mañana a las 9") → timestamp ISO.
- "prompt" debe ser ejecutable sin contexto de esta conversación: "resumen del estado del trader", no "eso que te pedí".
- Si el usuario pregunta qué tareas tiene → action=list.
- Si falta el cuándo o el qué → action=unclear con una clarification concreta.
- NUNCA inventes fechas: si el usuario no dio hora, pregunta.
- delivery=whatsapp por default."""


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
        try:
            parsed = self._parse_intent(query, tz_name)
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

    def _parse_intent(self, query: str, tz_name: str) -> dict:
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

        return f"Acción desconocida del parser: {action!r}. Reformula la petición."
