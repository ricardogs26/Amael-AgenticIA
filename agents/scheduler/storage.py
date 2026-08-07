"""
Storage del scheduler conversacional — tabla `user_jobs` en PostgreSQL.

Equivalente al `jobs.json` de Hermes Agent, pero en el store que ya existe:
un archivo JSON local no sobrevive a un pod y aquí puede haber más de una
réplica durante un rollout (maxSurge=1).

El `schedule` acepta dos formas, ambas validadas EN CÓDIGO antes de tocar la
tabla — el LLM traduce la intención, nunca decide qué es válido (misma lección
que trader 1.0.31):

  - cron de 5 campos ("0 15 * * *")          → job recurrente
  - timestamp ISO ("2026-08-10T09:00:00")    → one-shot; se deshabilita al correr
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("agents.scheduler.storage")

# Tope de jobs activos por usuario. Un LLM en un bucle desafortunado podría
# crear jobs sin parar; el tope convierte ese fallo en un error visible.
MAX_JOBS_PER_USER = int(os.environ.get("SCHEDULER_MAX_JOBS_PER_USER", "10"))

_DDL = """
CREATE TABLE IF NOT EXISTS user_jobs (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    schedule     TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'America/Mexico_City',
    delivery     TEXT NOT NULL DEFAULT 'whatsapp',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    one_shot     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    next_run_at  TIMESTAMPTZ NOT NULL,
    last_run_at  TIMESTAMPTZ,
    last_status  TEXT,
    run_count    INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_user_jobs_due  ON user_jobs (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_user_jobs_user ON user_jobs (user_id);
"""


@dataclass
class Job:
    id: int
    user_id: str
    title: str
    prompt: str
    schedule: str
    timezone: str
    delivery: str
    enabled: bool
    one_shot: bool
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None
    run_count: int = 0


def init_scheduler_db() -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    logger.info("[scheduler] Tabla user_jobs lista.")


# ── Validación del schedule ───────────────────────────────────────────────────

def compute_next_run(schedule: str, tz_name: str) -> tuple[datetime, bool]:
    """
    Valida el schedule y devuelve `(próxima ejecución en UTC, one_shot)`.

    Lanza ValueError con mensaje legible si no parsea o si un timestamp quedó
    en el pasado — ese mensaje es lo que Cassiel le repite al usuario.
    """
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise ValueError(f"Zona horaria desconocida: {tz_name!r}") from None

    texto = schedule.strip()

    # ¿Timestamp ISO? → one-shot
    try:
        cuando = datetime.fromisoformat(texto)
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=tz)
        cuando_utc = cuando.astimezone(UTC)
        if cuando_utc <= datetime.now(UTC):
            raise ValueError(
                f"La fecha {texto} ya pasó — un recordatorio one-shot debe ser futuro."
            )
        return cuando_utc, True
    except ValueError as exc:
        if "ya pasó" in str(exc):
            raise
        # No era ISO: se intenta como cron.

    from apscheduler.triggers.cron import CronTrigger
    try:
        trigger = CronTrigger.from_crontab(texto, timezone=tz)
    except Exception:
        raise ValueError(
            f"Schedule inválido: {texto!r}. Se acepta cron de 5 campos "
            f"(ej. '0 15 * * *') o un timestamp ISO futuro."
        ) from None

    siguiente = trigger.get_next_fire_time(None, datetime.now(tz))
    if siguiente is None:
        raise ValueError(f"El cron {texto!r} no produce ninguna ejecución futura.")
    return siguiente.astimezone(UTC), False


# ── CRUD ──────────────────────────────────────────────────────────────────────

_COLS = ("id, user_id, title, prompt, schedule, timezone, delivery, enabled, "
         "one_shot, next_run_at, last_run_at, last_status, run_count")

# SQL precompuesto a nivel módulo: solo interpola _COLS (constante de arriba);
# TODOS los valores van parametrizados en execute(). El nosec aplica aquí, en
# la construcción — dentro del string rompería el SQL y en cur.execute() bandit
# no lo lee (reporta sobre la línea del f-string).
_INSERT_SQL = f"""
    INSERT INTO user_jobs
        (user_id, title, prompt, schedule, timezone, delivery,
         one_shot, next_run_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING {_COLS}
"""  # nosec B608
_CLAIM_SQL = f"""
    SELECT {_COLS} FROM user_jobs
    WHERE enabled AND next_run_at <= NOW()
    ORDER BY next_run_at
    LIMIT %s
    FOR UPDATE SKIP LOCKED
"""  # nosec B608
_LIST_SQL      = f"SELECT {_COLS} FROM user_jobs WHERE user_id = %s ORDER BY id"  # nosec B608
_LIST_ON_SQL   = f"SELECT {_COLS} FROM user_jobs WHERE user_id = %s AND enabled ORDER BY id"  # nosec B608


def _row_to_job(row: tuple) -> Job:
    return Job(
        id=row[0], user_id=row[1], title=row[2], prompt=row[3], schedule=row[4],
        timezone=row[5], delivery=row[6], enabled=row[7], one_shot=row[8],
        next_run_at=row[9], last_run_at=row[10], last_status=row[11], run_count=row[12],
    )


def create_job(
    user_id: str, title: str, prompt: str, schedule: str,
    tz_name: str, delivery: str = "whatsapp",
) -> Job:
    from storage.postgres.client import get_connection

    if delivery not in ("whatsapp", "none"):
        raise ValueError(f"Entrega desconocida: {delivery!r} (whatsapp | none)")
    next_run, one_shot = compute_next_run(schedule, tz_name)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_jobs WHERE user_id = %s AND enabled",
                (user_id,),
            )
            if cur.fetchone()[0] >= MAX_JOBS_PER_USER:
                raise ValueError(
                    f"Ya tienes {MAX_JOBS_PER_USER} tareas activas — borra o pausa "
                    f"alguna antes de crear otra."
                )
            cur.execute(
                _INSERT_SQL,
                (user_id, title[:120], prompt[:2000], schedule, tz_name,
                 delivery, one_shot, next_run),
            )
            job = _row_to_job(cur.fetchone())
        conn.commit()
    logger.info(f"[scheduler] Job #{job.id} creado para {user_id}: {job.title!r}")
    return job


def list_jobs(user_id: str, include_disabled: bool = True) -> list[Job]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _LIST_SQL if include_disabled else _LIST_ON_SQL,
                (user_id,),
            )
            return [_row_to_job(r) for r in cur.fetchall()]


def find_job(user_id: str, ref: str) -> Job | None:
    """
    Localiza un job por id numérico o por substring del título (case-insensitive).
    Ambigüedad → ValueError con los candidatos, como hace Hermes: adivinar cuál
    borrar es peor que preguntar.
    """
    jobs = list_jobs(user_id)
    ref = ref.strip()
    if ref.isdigit():
        return next((j for j in jobs if j.id == int(ref)), None)
    candidatos = [j for j in jobs if ref.lower() in j.title.lower()]
    if len(candidatos) > 1:
        opciones = ", ".join(f"#{j.id} {j.title!r}" for j in candidatos)
        raise ValueError(f"Hay varias tareas que coinciden con {ref!r}: {opciones}")
    return candidatos[0] if candidatos else None


def set_enabled(job_id: int, user_id: str, enabled: bool) -> bool:
    """Al reanudar se recalcula next_run_at — si no, un job pausado una semana
    dispararía de inmediato todas las corridas perdidas."""
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            if enabled:
                cur.execute(
                    "SELECT schedule, timezone FROM user_jobs "
                    "WHERE id = %s AND user_id = %s",
                    (job_id, user_id),
                )
                row = cur.fetchone()
                if not row:
                    return False
                next_run, _ = compute_next_run(row[0], row[1])
                cur.execute(
                    "UPDATE user_jobs SET enabled = TRUE, next_run_at = %s "
                    "WHERE id = %s AND user_id = %s",
                    (next_run, job_id, user_id),
                )
            else:
                cur.execute(
                    "UPDATE user_jobs SET enabled = FALSE "
                    "WHERE id = %s AND user_id = %s",
                    (job_id, user_id),
                )
            cambiado = cur.rowcount > 0
        conn.commit()
    return cambiado


def delete_job(job_id: int, user_id: str) -> bool:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_jobs WHERE id = %s AND user_id = %s",
                (job_id, user_id),
            )
            borrado = cur.rowcount > 0
        conn.commit()
    return borrado


# ── Claim atómico para el tick ────────────────────────────────────────────────

def claim_due_jobs(limit: int = 10) -> list[Job]:
    """
    Toma los jobs vencidos y les fija su siguiente ejecución EN LA MISMA
    transacción, con FOR UPDATE SKIP LOCKED.

    Durante un rollout conviven dos réplicas (maxSurge=1) y ambas corren el
    tick: sin el claim atómico, el mismo recordatorio llegaría dos veces por
    WhatsApp. El que pierde la carrera simplemente no ve filas.
    """
    from storage.postgres.client import get_connection

    claimed: list[Job] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (limit,))
            for row in cur.fetchall():
                job = _row_to_job(row)
                if job.one_shot:
                    cur.execute(
                        "UPDATE user_jobs SET enabled = FALSE, last_run_at = NOW() "
                        "WHERE id = %s",
                        (job.id,),
                    )
                else:
                    try:
                        next_run, _ = compute_next_run(job.schedule, job.timezone)
                    except ValueError:
                        # Un cron que se volvió inválido (p.ej. TZ borrada del
                        # sistema) no debe reintentar cada minuto para siempre.
                        logger.error(
                            f"[scheduler] Job #{job.id} schedule inválido — deshabilitado."
                        )
                        cur.execute(
                            "UPDATE user_jobs SET enabled = FALSE, "
                            "last_status = 'schedule_invalido' WHERE id = %s",
                            (job.id,),
                        )
                        continue
                    cur.execute(
                        "UPDATE user_jobs SET next_run_at = %s, last_run_at = NOW() "
                        "WHERE id = %s",
                        (next_run, job.id),
                    )
                claimed.append(job)
        conn.commit()
    return claimed


def record_result(job_id: int, status: str) -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_jobs SET last_status = %s, run_count = run_count + 1 "
                "WHERE id = %s",
                (status[:80], job_id),
            )
        conn.commit()


def user_timezone(user_id: str) -> str:
    """Zona del usuario desde user_profile; default del laboratorio si no hay."""
    from storage.postgres.client import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT timezone FROM user_profile WHERE user_id = %s", (user_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception as exc:
        logger.debug(f"[scheduler] timezone no disponible para {user_id}: {exc}")
    return "America/Mexico_City"


def to_public(job: Job) -> dict[str, Any]:
    """Representación para el usuario — fechas en SU zona, no en UTC."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(job.timezone)
    except Exception:
        tz = UTC
    return {
        "id":       job.id,
        "title":    job.title,
        "schedule": job.schedule,
        "delivery": job.delivery,
        "enabled":  job.enabled,
        "one_shot": job.one_shot,
        "next_run": (job.next_run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                     if job.enabled else None),
        "runs":     job.run_count,
    }
