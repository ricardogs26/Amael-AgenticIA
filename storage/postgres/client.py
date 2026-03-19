"""
Cliente PostgreSQL con pool de conexiones y retry en el arranque.

Extraído de backend-ia/main.py. Singleton por proceso.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as pg_pool

logger = logging.getLogger("storage.postgres")

_connection_pool: pg_pool.ThreadedConnectionPool | None = None


def init_pool(
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    min_conn: int = 2,
    max_conn: int = 10,
    retries: int = 5,
    retry_delay: float = 3.0,
) -> pg_pool.ThreadedConnectionPool:
    """
    Inicializa el pool de conexiones PostgreSQL con reintentos.

    Args:
        retries:     Número máximo de intentos de conexión.
        retry_delay: Segundos entre intentos.

    Returns:
        ThreadedConnectionPool lista para usar.

    Raises:
        psycopg2.OperationalError: Si no se puede conectar tras todos los intentos.
    """
    global _connection_pool

    for attempt in range(1, retries + 1):
        try:
            _connection_pool = pg_pool.ThreadedConnectionPool(
                minconn=min_conn,
                maxconn=max_conn,
                host=host,
                port=port,
                dbname=dbname,
                user=user,
                password=password,
            )
            logger.info(
                f"Pool PostgreSQL inicializado (min={min_conn}, max={max_conn})",
                extra={"host": host, "port": port, "dbname": dbname},
            )
            return _connection_pool
        except psycopg2.OperationalError as e:
            logger.warning(
                f"Intento {attempt}/{retries} de conexión a PostgreSQL falló: {e}"
            )
            if attempt < retries:
                time.sleep(retry_delay)

    raise psycopg2.OperationalError(
        f"No se pudo conectar a PostgreSQL en {host}:{port} "
        f"tras {retries} intentos."
    )


def get_pool() -> pg_pool.ThreadedConnectionPool:
    """Retorna el pool existente. Lanza error si no fue inicializado."""
    if _connection_pool is None:
        raise RuntimeError(
            "El pool PostgreSQL no está inicializado. "
            "Llama a init_pool() primero."
        )
    return _connection_pool


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Context manager que obtiene/retorna una conexión del pool.

    Uso:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")
    """
    conn = get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().putconn(conn)


def health_check() -> bool:
    """Verifica que el pool está activo y puede ejecutar una query simple."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"Health check PostgreSQL falló: {e}")
        return False


def close_pool() -> None:
    """Cierra todas las conexiones del pool (usar en shutdown de la app)."""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("Pool PostgreSQL cerrado.")
