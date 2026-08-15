"""
Storage de tareas pendientes — tabla `user_tasks` en PostgreSQL.

Hermana de storage.py (user_jobs): un job es un cron que ejecuta un prompt;
una tarea tiene estado, prioridad y ciclo de vida. Toda decisión validable
vive AQUÍ, no en el prompt de Cassiel.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger("agents.scheduler.tasks_storage")

CATEGORIES = ("personal", "laboral")
PRIORITIES = ("alta", "media", "baja")
STATUSES   = ("pending", "done", "cancelled")
MAX_PENDING_PER_USER = int(os.environ.get("TASKS_MAX_PENDING_PER_USER", "50"))

_DDL = """
CREATE TABLE IF NOT EXISTS user_tasks (
    id                SERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL DEFAULT 'personal'
                      CHECK (category IN ('personal','laboral')),
    priority          TEXT NOT NULL DEFAULT 'media'
                      CHECK (priority IN ('alta','media','baja')),
    estimated_minutes INT,
    due_date          DATE,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','done','cancelled')),
    needs_scheduling  BOOLEAN NOT NULL DEFAULT FALSE,
    calendar_event_id TEXT,
    last_nudge_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_tasks_user ON user_tasks (user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_tasks_due  ON user_tasks (status, due_date);
"""


@dataclass
class Task:
    id: int
    user_id: str
    title: str
    description: str
    category: str
    priority: str
    estimated_minutes: int | None
    due_date: date | None
    status: str
    needs_scheduling: bool
    calendar_event_id: str | None
    last_nudge_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


def init_tasks_db() -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    logger.info("[tasks] Tabla user_tasks lista.")


def validate_task_fields(category: str, priority: str, status: str) -> None:
    """El LLM propone estos campos; aquí se decide si son válidos."""
    if category not in CATEGORIES:
        raise ValueError(f"Categoría inválida: {category!r} ({'|'.join(CATEGORIES)})")
    if priority not in PRIORITIES:
        raise ValueError(f"Prioridad inválida: {priority!r} ({'|'.join(PRIORITIES)})")
    if status not in STATUSES:
        raise ValueError(f"Estado inválido: {status!r} ({'|'.join(STATUSES)})")


_COLS = ("id, user_id, title, description, category, priority, estimated_minutes, "
         "due_date, status, needs_scheduling, calendar_event_id, last_nudge_at, "
         "created_at, completed_at")

_INSERT_SQL = f"""
    INSERT INTO user_tasks
        (user_id, title, description, category, priority, estimated_minutes,
         due_date, needs_scheduling)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING {_COLS}
"""  # nosec B608
_LIST_SQL = f"""
    SELECT {_COLS} FROM user_tasks
    WHERE user_id = %s AND status = 'pending'
    ORDER BY id
"""  # nosec B608


def _row_to_task(row: tuple) -> Task:
    return Task(*row)


def create_task(
    user_id: str, title: str, *, description: str = "",
    category: str = "personal", priority: str = "media",
    estimated_minutes: int | None = None, due_date: date | None = None,
    needs_scheduling: bool = False,
) -> Task:
    from storage.postgres.client import get_connection

    validate_task_fields(category, priority, "pending")
    if not title.strip():
        raise ValueError("La tarea necesita un título.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_tasks WHERE user_id = %s AND status = 'pending'",
                (user_id,),
            )
            if cur.fetchone()[0] >= MAX_PENDING_PER_USER:
                raise ValueError(
                    f"Ya tienes {MAX_PENDING_PER_USER} pendientes — cierra o "
                    f"cancela algunas antes de anotar más."
                )
            cur.execute(
                _INSERT_SQL,
                (user_id, title.strip()[:120], description[:1000], category,
                 priority, estimated_minutes, due_date, needs_scheduling),
            )
            task = _row_to_task(cur.fetchone())
        conn.commit()
    logger.info(f"[tasks] Tarea #{task.id} creada para {user_id}: {task.title!r}")
    return task


def list_pending(user_id: str) -> list[Task]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_LIST_SQL, (user_id,))
            return [_row_to_task(r) for r in cur.fetchall()]


def sort_key(task: Task, today: date) -> tuple:
    """Orden EFECTIVO (spec §1): vencida/hoy > alta > media > baja;
    desempate por due_date más próxima, luego created_at."""
    overdue = task.due_date is not None and task.due_date <= today
    return (
        0 if overdue else 1,
        PRIORITIES.index(task.priority),
        task.due_date or date.max,
        task.created_at,
    )


def sorted_pending(tasks: list[Task], today: date) -> list[Task]:
    return sorted(tasks, key=lambda t: sort_key(t, today))


def match_tasks(tasks: list[Task], ref: str) -> list[Task]:
    """Busca tareas por ID numérico exacto o substring case-insensitive en
    título. Devuelve lista (0, 1, o más matches — ambigüedad resuelta arriba)."""
    ref = ref.strip()
    if ref.isdigit():
        return [t for t in tasks if t.id == int(ref)]
    return [t for t in tasks if ref.lower() in t.title.lower()]


def find_task(user_id: str, ref: str) -> Task | None:
    """Busca UNA tarea por ref (ID numérico o substring en título).

    - 1 candidato → Task
    - 0 → None
    - 2+ → ValueError con opciones (adivinar cuál cerrar es peor que preguntar,
            patrón de find_job en storage.py)
    """
    candidatos = match_tasks(list_pending(user_id), ref)
    if len(candidatos) > 1:
        opciones = ", ".join(f"#{t.id} {t.title!r}" for t in candidatos)
        raise ValueError(
            f"Hay varias pendientes que coinciden con {ref!r}: {opciones}"
        )
    return candidatos[0] if candidatos else None


def set_status(task_id: int, user_id: str, status: str) -> bool:
    """Fija el estado de una tarea. Si es 'done', también fija completed_at al
    NOW() actual."""
    validate_task_fields("personal", "media", status)  # solo valida status
    from storage.postgres.client import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET status = %s, "
                "completed_at = CASE WHEN %s = 'done' THEN NOW() "
                "ELSE completed_at END "
                "WHERE id = %s AND user_id = %s AND status = 'pending'",
                (status, status, task_id, user_id),
            )
            cambiado = cur.rowcount > 0
        conn.commit()
    return cambiado


def postpone_task(task_id: int, user_id: str, new_due: date) -> bool:
    """Cambia la due_date de una tarea a new_due y resetea last_nudge_at a NULL
    (para que el scheduler la considere nuevamente)."""
    from storage.postgres.client import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET due_date = %s, last_nudge_at = NULL "
                "WHERE id = %s AND user_id = %s AND status = 'pending'",
                (new_due, task_id, user_id),
            )
            cambiado = cur.rowcount > 0
        conn.commit()
    return cambiado


def nudge_eligible(task: Task, today: date) -> bool:
    """Spec §3: el día del due SIEMPRE avisa; vencidas: alta diario,
    media cada 3 días, baja solo brief (nunca nudge suelto).
    Máx 1 nudge por tarea al día vía last_nudge_at."""
    if task.status != "pending" or task.due_date is None or task.due_date > today:
        return False
    if task.last_nudge_at is not None and task.last_nudge_at.date() >= today:
        return False
    if task.due_date == today:
        return True
    dias_vencida = (today - task.due_date).days
    if task.priority == "alta":
        return True
    if task.priority == "media":
        return dias_vencida % 3 == 0
    return False


def select_nudges(tasks: list[Task], today: date, cap: int = 3) -> list[Task]:
    elegibles = [t for t in tasks if nudge_eligible(t, today)]
    return sorted_pending(elegibles, today)[:cap]


def mark_nudged(task_id: int) -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET last_nudge_at = NOW() WHERE id = %s",
                (task_id,),
            )
        conn.commit()


_ALL_DUE_SQL = f"""
    SELECT {_COLS} FROM user_tasks
    WHERE status = 'pending' AND due_date IS NOT NULL
"""  # nosec B608


def all_pending_with_due() -> list[Task]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_ALL_DUE_SQL)
            return [_row_to_task(r) for r in cur.fetchall()]
