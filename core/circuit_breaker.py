"""
Circuit breaker ligero respaldado en Redis.

Estados: CLOSED (normal) → OPEN (disparado) → HALF_OPEN (prueba)

Parámetros por defecto:
  failure_threshold  = 5  fallos para abrir
  recovery_timeout   = 30s para pasar a HALF_OPEN
  half_open_max_calls = 1  llamada de prueba en HALF_OPEN

Uso:
    cb = CircuitBreaker("k8s_agent", redis_client)
    if cb.is_open():
        return "Servicio no disponible temporalmente."
    try:
        result = call_service()
        cb.record_success()
    except Exception as exc:
        cb.record_failure()
        raise
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("core.circuit_breaker")

_STATE_CLOSED    = "closed"
_STATE_OPEN      = "open"
_STATE_HALF_OPEN = "half_open"

_KEY_PREFIX = "circuit_breaker:"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        redis_client,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
    ) -> None:
        self.name             = name
        self._redis           = redis_client
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._key_state        = f"{_KEY_PREFIX}{name}:state"
        self._key_failures     = f"{_KEY_PREFIX}{name}:failures"
        self._key_opened_at    = f"{_KEY_PREFIX}{name}:opened_at"

    def _get_state(self) -> str:
        try:
            state = self._redis.get(self._key_state)
            return state.decode() if isinstance(state, bytes) else (state or _STATE_CLOSED)
        except Exception:
            return _STATE_CLOSED  # fail-open si Redis no disponible

    def is_open(self) -> bool:
        """
        Retorna True si el circuito está OPEN (debe rechazar la llamada).
        Transiciona OPEN → HALF_OPEN si ha pasado recovery_timeout.
        """
        try:
            state = self._get_state()
            if state == _STATE_CLOSED:
                return False
            if state == _STATE_OPEN:
                opened_at = self._redis.get(self._key_opened_at)
                if opened_at:
                    elapsed = time.time() - float(opened_at)
                    if elapsed >= self.recovery_timeout:
                        self._redis.set(self._key_state, _STATE_HALF_OPEN)
                        logger.info(f"[circuit_breaker] {self.name}: OPEN → HALF_OPEN")
                        return False  # deja pasar la llamada de prueba
                return True
            # HALF_OPEN: dejar pasar
            return False
        except Exception:
            return False  # fail-open

    def record_success(self) -> None:
        """Llamada exitosa — cierra el circuito si estaba en HALF_OPEN."""
        try:
            state = self._get_state()
            if state in (_STATE_HALF_OPEN, _STATE_OPEN):
                self._redis.set(self._key_state, _STATE_CLOSED)
                self._redis.delete(self._key_failures, self._key_opened_at)
                logger.info(f"[circuit_breaker] {self.name}: → CLOSED (recovered)")
            else:
                # En estado CLOSED, resetear contador de fallos
                self._redis.delete(self._key_failures)
        except Exception:
            pass

    def record_failure(self) -> None:
        """Llamada fallida — incrementa contador y abre si supera threshold."""
        try:
            failures = self._redis.incr(self._key_failures)
            self._redis.expire(self._key_failures, self.recovery_timeout * 4)
            if int(failures) >= self.failure_threshold:
                self._redis.set(self._key_state, _STATE_OPEN)
                self._redis.set(self._key_opened_at, str(time.time()))
                self._redis.expire(self._key_state, self.recovery_timeout * 10)
                logger.warning(
                    f"[circuit_breaker] {self.name}: OPEN after {failures} failures"
                )
        except Exception:
            pass

    def get_status(self) -> dict:
        """Retorna estado actual del circuit breaker para health checks."""
        try:
            state    = self._get_state()
            failures = self._redis.get(self._key_failures)
            return {
                "state":    state,
                "failures": int(failures) if failures else 0,
            }
        except Exception:
            return {"state": "unknown", "failures": 0}
