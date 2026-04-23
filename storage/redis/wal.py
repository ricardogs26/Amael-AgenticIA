"""
storage.redis.wal — Write-Ahead Log genérico sobre Redis.

Usado como fallback cuando un servicio downstream (ej. camael-service) no está
disponible. El productor encola el payload; el consumidor lo drena cuando
se recupera conectividad.

Keys: wal:camael:{topic}:{key}   — namespace fijo "camael" (único consumidor hoy).
TTL:  24h por default (86400s) — eventos más viejos se pierden (idempotencia
      externa por incident_key debe proteger contra replays tardíos).

Topics soportados (convención, no enforced):
  - handoff       — Raphael → Camael handoff
  - rfc_update    — Raphael → Camael PATCH RFC post-verificación

Idempotencia: por `key` (incident_key, sys_id). Mismo key sobrescribe.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("storage.redis.wal")

_KEY_TEMPLATE = "wal:camael:{topic}:{key}"
_DEFAULT_TTL_SECONDS = 86400  # 24h


def _make_key(topic: str, key: str) -> str:
    return _KEY_TEMPLATE.format(topic=topic, key=key)


def enqueue(
    topic: str,
    key: str,
    payload: dict[str, Any],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> bool:
    """
    Encola un evento en el WAL. Idempotente por `key`.
    Retorna True si se persistió, False si Redis falla.
    """
    try:
        from storage.redis.client import get_client
        r = get_client()
        full_key = _make_key(topic, key)
        r.set(full_key, json.dumps(payload), ex=ttl_seconds)
        logger.warning(
            f"[wal] enqueued {full_key} (TTL {ttl_seconds}s)"
        )
        return True
    except Exception as exc:
        logger.error(f"[wal] enqueue FALLÓ topic={topic} key={key}: {exc}")
        return False


def drain(
    topic: str,
    consumer: Callable[[dict[str, Any]], bool],
) -> int:
    """
    Drena todas las entradas pendientes del topic.
    `consumer(payload)` debe retornar True en éxito (→ key se borra) o
    False/raise en fallo (→ key se conserva para siguiente tick).

    Retorna el número de entradas procesadas con éxito.
    """
    try:
        from storage.redis.client import get_client
        r = get_client()
        pattern = _make_key(topic, "*")
        keys = r.keys(pattern) or []
    except Exception as exc:
        logger.error(f"[wal] drain connect FALLÓ topic={topic}: {exc}")
        return 0

    ok = 0
    for full_key in keys:
        full_key_str = full_key if isinstance(full_key, str) else full_key.decode()
        try:
            raw = r.get(full_key_str)
            if raw is None:
                continue
            raw_str = raw if isinstance(raw, str) else raw.decode()
            payload = json.loads(raw_str)
            success = consumer(payload)
            if success:
                r.delete(full_key_str)
                ok += 1
                logger.info(f"[wal] drained {full_key_str}")
        except Exception as exc:
            logger.warning(f"[wal] consumer error on {full_key_str}: {exc}")
            # No borrar — retry en próximo tick
    return ok


def pending_count(topic: str) -> int:
    """Cuenta entradas pendientes del topic (para métricas / alertas)."""
    try:
        from storage.redis.client import get_client
        r = get_client()
        keys = r.keys(_make_key(topic, "*")) or []
        return len(keys)
    except Exception:
        return 0
