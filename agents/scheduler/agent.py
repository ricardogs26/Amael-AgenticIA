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
import re
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
- «/pendientes» o «qué tengo pendiente» → task_list (filter si lo dijo).
- Si el mensaje trae [Contexto: …], es la continuación de esa petición — únelos."""


_FOLLOWUP_TTL_S      = 600
_FOLLOWUP_MAX_CHARS  = 1500   # tope duro del texto guardado — nunca crece sin límite
_FOLLOWUP_MAX_ROUNDS = 2      # cuántas veces Cassiel puede volver a preguntar

_FOLLOWUP_WRAP_MARKER = "[Contexto: el usuario respondía a Cassiel sobre: "

# Marcador interno (nunca llega al usuario ni al LLM) que viaja PEGADO al
# principio del texto fusionado para que CassielAgent.execute() sepa en qué
# ronda de followup está sin tener que ir a Redis otra vez — pop_followup ya
# fue destructivo (GETDEL) para cuando el turno llega aquí.
_FOLLOWUP_MARKER_RE = re.compile(r"^\[\[CASSIEL_FOLLOWUP:n=(\d+)\]\]\n")

_FOLLOWUP_ESCAPE_MSG = (
    "No logré entenderlo tras varias vueltas 🙏 Mándamelo en una sola frase, "
    "por ejemplo: «anota: ir por los útiles escolares el 22 de agosto» o "
    "«recuérdame el viernes a las 9 revisar el aviso»."
)

_FOLLOWUP_ANSWERED_NOTE = (
    "El usuario YA respondió a tu pregunta anterior — actúa con lo que "
    "tienes; NO vuelvas a preguntar lo mismo."
)


def _strip_followup_wrapper(text: str) -> str:
    """Pure. Si `text` contiene el wrapper de contexto de Cassiel — una vez o
    anidado varias veces, en cualquier posición — se queda solo con el
    núcleo original. Sin esto, cada ronda reenvolvía el wrapper anterior:
    `[Contexto: [Contexto: [Contexto: …]]]` hasta ahogar al LLM."""
    result = text
    for _ in range(10):   # tope defensivo contra anidamiento patológico
        idx = result.find(_FOLLOWUP_WRAP_MARKER)
        if idx == -1:
            break
        start = idx + len(_FOLLOWUP_WRAP_MARKER)
        end = result.rfind("]")
        if end == -1 or end < start:
            break
        result = result[start:end]
    return result.strip()


def _followup_payload(prev_query: str, round_n: int) -> dict | None:
    """Pure. Arma el valor a guardar para el followup de Cassiel, o None SOLO
    si `round_n` excede el tope de rondas (loop-breaker) — esa es la única
    señal que debe cortar el flujo con la salida de escape. Un texto vacío
    no es un caso de tope: se guarda tal cual (payload["q"] == ""), y es
    quien llama (`_set_followup`) el que decide no pegarle a Redis por nada.
    Desenvuelve wrappers previos y trunca al inicio del texto — las fechas
    de un aviso viven al principio."""
    if round_n > _FOLLOWUP_MAX_ROUNDS:
        return None
    core = _strip_followup_wrapper(prev_query).strip()[:_FOLLOWUP_MAX_CHARS]
    return {"q": core, "n": round_n}


def _parse_followup_value(raw) -> dict:
    """Pure. El valor viejo guardado en Redis era el string crudo (sin
    ronda) — se trata como ronda 1 para no romper followups en vuelo durante
    el despliegue."""
    if isinstance(raw, dict):
        return {"q": str(raw.get("q") or ""), "n": int(raw.get("n") or 1)}
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"q": text, "n": 1}
    if isinstance(data, dict) and "q" in data:
        return {"q": str(data.get("q") or ""), "n": int(data.get("n") or 1)}
    return {"q": text, "n": 1}


def merge_followup(question: str, prev) -> str:
    """Combina la respuesta del usuario con la petición previa sobre la que
    Cassiel preguntó. Orden: la RESPUESTA va primero (es la señal), el
    contexto después (es referencia) — antes iba el aviso largo primero y
    la respuesta corta se perdía al fondo del prompt. Nunca reenvuelve un
    wrapper ya existente ni deja crecer el texto sin límite."""
    parsed = prev if isinstance(prev, dict) and "n" in prev and "q" in prev \
        else _parse_followup_value(prev)
    core = _strip_followup_wrapper(parsed["q"]).strip()[:_FOLLOWUP_MAX_CHARS]
    body = f"{question}\n\n{_FOLLOWUP_WRAP_MARKER}{core}]"
    return f"[[CASSIEL_FOLLOWUP:n={parsed['n']}]]\n{body}"


def _extract_followup_round(query: str) -> tuple[str, int]:
    """Pure. Quita el marcador interno de ronda (si lo hay) y devuelve
    (texto_limpio, ronda_previa). ronda_previa=0 si este turno no viene de
    un merge de followup."""
    m = _FOLLOWUP_MARKER_RE.match(query)
    if not m:
        return query, 0
    return _FOLLOWUP_MARKER_RE.sub("", query, count=1), int(m.group(1))


def pop_followup(user: str) -> dict | None:
    """Consume (GETDEL — un solo uso) el followup pendiente del usuario.
    Best-effort: sin Redis devuelve None, jamás lanza."""
    try:
        from storage.redis.client import get_redis_client
        raw = get_redis_client().getdel(f"cassiel:followup:{user}")
        if raw is None:
            return None
        return _parse_followup_value(raw)
    except Exception:
        return None


@AgentRegistry.register
class CassielAgent(BaseAgent):
    """Cassiel — recordatorios y tareas programadas por chat."""

    name         = "cassiel"
    role         = "Scheduler conversacional — recordatorios y tareas programadas"
    version      = "1.0.0"
    capabilities = ["job_create", "job_list", "job_pause", "job_resume", "job_delete"]

    # Última petición del usuario en este turno — la guardan las ramas que
    # responden con una pregunta, para que la siguiente respuesta vuelva aquí.
    _last_query: str = ""
    # Ronda de followup que se guardaría SI esta rama vuelve a preguntar
    # (previa + 1). Default 1: turno que no viene de un merge de followup.
    _next_followup_round: int = 1

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

        query, prev_round = _extract_followup_round(query)
        self._last_query = query
        self._next_followup_round = prev_round + 1
        tz_name = storage.user_timezone(user_email)
        pending = self._pop_pending_question(user_email)
        extra_lines = []
        if pending:
            extra_lines.append(
                f"Pregunta pendiente al usuario: fecha para la tarea "
                f"#{pending.get('task_id')}. Si este mensaje la responde, "
                f"action=task_postpone con task_ref={pending.get('task_id')} y new_due."
            )
        if prev_round > 0:
            extra_lines.append(_FOLLOWUP_ANSWERED_NOTE)
        extra_context = "\n".join(extra_lines) or None
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
            if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                return _FOLLOWUP_ESCAPE_MSG
            return str(parsed.get("clarification")
                       or "¿Qué quieres que programe y cuándo?")

        if action == "create":
            title    = str(parsed.get("title") or "").strip()
            prompt   = str(parsed.get("prompt") or "").strip()
            schedule = str(parsed.get("schedule") or "").strip()
            delivery = str(parsed.get("delivery") or "whatsapp").strip().lower()
            if not (title and prompt and schedule):
                if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                    return _FOLLOWUP_ESCAPE_MSG
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
                if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                    return _FOLLOWUP_ESCAPE_MSG
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
                    if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                        return _FOLLOWUP_ESCAPE_MSG
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
                    if self._set_followup(user_email, self._last_query, self._next_followup_round):
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
                    lineas.append(f"{marca} #{t.id} {t.title} [{t.category}/"
                                  f"{t.priority}]{mins}{fecha}")
                return "Tus pendientes:\n" + "\n".join(lineas)

            # task_done / task_cancel / task_postpone
            ref = str(parsed.get("task_ref") or "").strip()
            if not ref:
                if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                    return _FOLLOWUP_ESCAPE_MSG
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
            # fecha inválida): el mensaje ya es legible. La ambigüedad de
            # find_task («Hay varias…») es una pregunta al usuario → followup.
            if "hay varias" in str(exc).lower():
                if not self._set_followup(user_email, self._last_query, self._next_followup_round):
                    return _FOLLOWUP_ESCAPE_MSG
            return str(exc)

    _PENDING_TTL_S = 600

    def _set_followup(self, user: str, prev_query: str, round_n: int = 1) -> bool:
        """Marca que Cassiel dejó una pregunta abierta: el próximo mensaje del
        usuario se rutea de vuelta a Cassiel con `prev_query` como contexto.
        Si la query trae bloques inyectados por chat.py, guarda solo la
        pregunta real del usuario. Devuelve False (y NO guarda) si `round_n`
        excede el tope de rondas — el llamador debe usar la salida
        determinista (`_FOLLOWUP_ESCAPE_MSG`) en vez de volver a preguntar."""
        if "[Pregunta actual]\n" in prev_query:
            prev_query = prev_query.rsplit("[Pregunta actual]\n", 1)[-1]
        payload = _followup_payload(prev_query, round_n)
        if payload is None:
            return False
        if not payload["q"]:
            return True   # nada útil que guardar, pero no es el loop-breaker
        try:
            from storage.redis.client import get_redis_client
            get_redis_client().setex(
                f"cassiel:followup:{user}", _FOLLOWUP_TTL_S, json.dumps(payload),
            )
        except Exception as exc:
            logger.debug(f"[cassiel] followup no guardado: {exc}")
        return True

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
