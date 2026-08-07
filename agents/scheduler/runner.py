"""
Tick del scheduler — ejecuta los `user_jobs` vencidos.

Cada 60 s (patrón del loop SRE) toma los jobs vencidos con claim atómico y corre
el prompt de cada uno por el AgentDispatcher COMO SI el usuario lo hubiera
escrito en ese momento: «resumen del trader» pasa por el router y cae en el
agente del trader, igual que en el chat. Es el diseño de Hermes (sesión fresca
por job) sobre las piezas que ya existen.

Con AGENTS_MODE=remote el backend no arranca ningún APScheduler (el loop SRE
vive en raphael-service), así que este módulo trae el suyo propio y se arranca
incondicionalmente desde el lifespan.
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("agents.scheduler.runner")

_TICK_SECONDS   = int(os.environ.get("SCHEDULER_TICK_SECONDS", "60"))
# Timeout por job: un prompt que cae al pipeline LangGraph puede tomar minutos
# en el 14b; sin tope, un job colgado bloquearía el tick para siempre.
_JOB_TIMEOUT_S  = int(os.environ.get("SCHEDULER_JOB_TIMEOUT_S", "300"))

_scheduler = None


def start_scheduler_loop() -> None:
    """Arranca el tick. Llamar una vez desde el lifespan del backend."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick, "interval", seconds=_TICK_SECONDS,
        id="user_jobs_tick", replace_existing=True,
        max_instances=1,   # un tick largo no debe encimarse con el siguiente
    )
    # Consolidación de memoria (B1) — 03:30 hora de México, media hora después
    # del consolidador de runbooks para no encimarse con él en el ollama-cpu.
    # La trampa que NO se repite: el nivel 3 de runbooks estuvo roto desde su
    # creación porque ningún add_job lo agendaba pese a lo que decía la doc.
    # Por eso el log de abajo imprime el next_run_time real de cada job.
    _scheduler.add_job(
        _memory_consolidation, "cron", hour=3, minute=30,
        timezone="America/Mexico_City",
        id="memory_consolidation", replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    for job in _scheduler.get_jobs():
        logger.info(f"[scheduler] Job agendado: {job.id} → próxima {job.next_run_time}")


def stop_scheduler_loop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _acquire_daily_lock(nombre: str, ttl_s: int = 3300) -> bool:
    """
    Lock diario entre réplicas vía Redis SET NX. TTL de 55 min: menor que las
    24 h entre corridas (mañana el lock ya expiró) y mayor que cualquier
    consolidación razonable. Si Redis no responde, se corre igual — duplicar
    una síntesis es preferible a no consolidar nunca por un Redis caído.
    """
    try:
        from storage.redis.client import get_redis_client
        return bool(get_redis_client().set(
            f"scheduler:lock:{nombre}", "1", nx=True, ex=ttl_s,
        ))
    except Exception as exc:
        logger.warning(f"[scheduler] lock {nombre} no disponible ({exc}) — se corre igual.")
        return True


async def _memory_consolidation() -> None:
    """Destila los episodios de memoria en hechos. Trabajo sync (LLM del tier
    profundo, ~minutos) → a un thread para no bloquear el event loop."""
    # El HPA fija minReplicas=2 (el replicas:1 del manifest es letra muerta),
    # así que este cron dispara en DOS pods a la vez — verificado el 7-ago:
    # ambos corrieron a las 09:30:00.014 con 130 µs de diferencia. Dos
    # síntesis simultáneas duplicarían hechos y se pisarían los borrados.
    # Mismo problema que user_jobs resuelve con SKIP LOCKED; aquí basta un
    # SET NX: el que llega segundo no debe esperar, debe no correr.
    if not _acquire_daily_lock("memory_consolidation"):
        logger.info("[scheduler] Consolidación de memoria: otra réplica la tomó.")
        return
    from agents.memory_agent.consolidator import run_consolidation
    try:
        resultados = await asyncio.to_thread(run_consolidation)
        hechos = sum(r.get("facts", 0) for r in resultados)
        logger.info(
            f"[scheduler] Consolidación de memoria: {len(resultados)} colección(es), "
            f"{hechos} hecho(s) destilados."
        )
    except Exception as exc:
        logger.error(f"[scheduler] Consolidación de memoria falló: {exc}")


async def _tick() -> None:
    from agents.scheduler import storage
    try:
        due = storage.claim_due_jobs()
    except Exception as exc:
        logger.error(f"[scheduler] claim falló: {exc}")
        return
    if not due:
        return
    logger.info(f"[scheduler] {len(due)} job(s) vencidos.")
    # Secuencial a propósito: los jobs comparten un solo GPU/LLM y encimarlos
    # solo alarga a todos. El claim ya fijó next_run_at, no hay prisa.
    for job in due:
        try:
            await asyncio.wait_for(_run_job(job), timeout=_JOB_TIMEOUT_S)
            storage.record_result(job.id, "ok")
        except TimeoutError:
            logger.error(f"[scheduler] Job #{job.id} superó {_JOB_TIMEOUT_S}s.")
            storage.record_result(job.id, "timeout")
        except Exception as exc:
            logger.error(f"[scheduler] Job #{job.id} falló: {exc}")
            storage.record_result(job.id, f"error: {exc}")


async def _run_job(job) -> None:
    """Ejecuta el prompt del job por el flujo normal de chat y entrega."""
    from interfaces.api.routers.chat import _build_tools_map
    from orchestration.agent_dispatcher import AgentDispatcher
    from orchestration.agent_router import AgentRouter

    decision = await AgentRouter().route(job.prompt)
    result   = await AgentDispatcher().dispatch(
        question=job.prompt,
        user_id=job.user_id,
        tools_map=_build_tools_map(job.user_id),
        routing_decision=decision,
        request_id=f"job-{job.id}",
    )
    answer = (result.get("final_answer") or "").strip()
    if not answer:
        raise RuntimeError("el dispatcher no devolvió respuesta")

    from security.sanitizer import sanitize_output
    answer = sanitize_output(answer)

    if job.delivery == "whatsapp":
        _deliver_whatsapp(job, answer)
    else:
        logger.info(f"[scheduler] Job #{job.id} completado (sin entrega).")


def _deliver_whatsapp(job, text: str) -> None:
    """
    Entrega por el bridge, resolviendo el número igual que el brief diario
    (planner.py): identity_type='whatsapp', ORDER BY id, y SIN fallback — el
    resultado de la tarea de alguien no se manda al teléfono de nadie más.
    """
    import httpx

    from config.settings import settings
    from storage.postgres.client import get_connection

    phone: str | None = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT identity_value FROM user_identities
                WHERE canonical_user_id = %s AND identity_type = 'whatsapp'
                ORDER BY id LIMIT 1
                """,
                (job.user_id,),
            )
            row = cur.fetchone()
            if row:
                phone = row[0]
    if not phone:
        raise RuntimeError(
            f"{job.user_id} no tiene WhatsApp registrado — resultado no entregado"
        )

    wa_url = getattr(settings, "whatsapp_bridge_url", "http://whatsapp-bridge-service:3000")
    resp = httpx.post(
        f"{wa_url}/send",
        json={"phoneNumber": phone, "text": f"⏰ *{job.title}*\n\n{text}"},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"bridge /send respondió {resp.status_code}")
    logger.info(f"[scheduler] Job #{job.id} entregado a {phone}.")
