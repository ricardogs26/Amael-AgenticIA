"""
Cliente Redis singleton para la plataforma.

Usado por:
  - Rate limiting (contadores por usuario)
  - Session context (estado de conversación activa)
  - SRE dedup (incidentes duplicados con TTL 1h)
  - Maintenance windows (ventanas de mantenimiento SRE)
  - Supervisor feedback (historial de decisiones por usuario)
"""
from __future__ import annotations

import logging
from typing import Optional

import redis as redis_lib

logger = logging.getLogger("storage.redis")

_client: Optional[redis_lib.Redis] = None


def init_client(
    host: str,
    port: int = 6379,
    db: int = 0,
    decode_responses: bool = True,
) -> redis_lib.Redis:
    """
    Inicializa el cliente Redis singleton.

    Args:
        decode_responses: True para que las respuestas sean strings (no bytes).
    """
    global _client
    _client = redis_lib.Redis(
        host=host,
        port=port,
        db=db,
        decode_responses=decode_responses,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
    )
    # Verificar conectividad al inicializar
    _client.ping()
    logger.info(
        "Cliente Redis inicializado",
        extra={"host": host, "port": port, "db": db},
    )
    return _client


def get_client() -> redis_lib.Redis:
    """Retorna el cliente Redis existente."""
    if _client is None:
        raise RuntimeError(
            "El cliente Redis no está inicializado. "
            "Llama a init_client() primero."
        )
    return _client


def health_check() -> bool:
    """Verifica conectividad con Redis."""
    try:
        return get_client().ping()
    except Exception as e:
        logger.error(f"Health check Redis falló: {e}")
        return False
