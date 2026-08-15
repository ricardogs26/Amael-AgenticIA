"""
Bloque de perfil estable — Fase B2 del plan Hermes.

Los hechos destilados (kind="fact") se inyectan COMPLETOS al prompt de cada
conversación. Es la lección central de Hermes: el coseno no sirve para
preferencias — recupera fragmentos si la pregunta se parece léxicamente, y
«prefiere respuestas cortas» debe aplicar también cuando la pregunta es sobre
Kong. Lo estable se inyecta siempre; lo episódico sí se recupera por similitud.

El tope es de CARACTERES y se aplica EN CÓDIGO — nunca se le pide al LLM que
resuma para caber (lección trader 1.0.31/1.0.4). La ruta rápida usa qwen3:1.7b
con 4 096 tokens de contexto y un prompt medido de ~330 tokens: 900 chars son
~250 tokens, presupuesto deliberado.

Caché en Redis con TTL: leer Qdrant en cada mensaje sería pagar un scroll por
turno para datos que cambian una vez al día (el consolidador corre a las 03:30).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("agents.memory.profile")

_QDRANT_URL   = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
MAX_BLOCK_CHARS = int(os.environ.get("MEMORY_PROFILE_MAX_CHARS", "900"))
_CACHE_TTL_S    = int(os.environ.get("MEMORY_PROFILE_CACHE_TTL_S", "3600"))
_CACHE_PREFIX   = "profile_block:"
# Centinela para cachear también el caso «sin hechos»: sin esto, cada mensaje
# de un usuario sin perfil pagaría el scroll a Qdrant completo.
_EMPTY_SENTINEL = "\x00empty"

_HEADER = "Contexto del usuario (no lo cites ni menciones; úsalo solo si es relevante):"


def _sanitize_user_id(user_id: str) -> str:
    # Mismo saneo que usa Zaphkiel para nombrar la colección.
    return user_id.replace("@", "_at_").replace(".", "_dot_")


def collection_for(user_id: str) -> str:
    return f"memory_{_sanitize_user_id(user_id)}"


def _redis():
    from storage.redis.client import get_redis_client
    return get_redis_client()


def _fetch_facts(user_id: str) -> list[dict]:
    """Hechos del usuario desde Qdrant (scroll con filtro kind=fact)."""
    import json
    import urllib.request

    body = {
        "limit": 100,
        "with_payload": True,
        "with_vector": False,
        "filter": {"must": [{"key": "kind", "match": {"value": "fact"}}]},
    }
    if not _QDRANT_URL.startswith(("http://", "https://")):
        raise ValueError(f"QDRANT_URL con esquema no permitido: {_QDRANT_URL!r}")
    req = urllib.request.Request(
        f"{_QDRANT_URL}/collections/{collection_for(user_id)}/points/scroll",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310 — esquema validado arriba
        result = json.load(resp).get("result") or {}
    return [p.get("payload") or {} for p in result.get("points", [])]


def render_profile_block(user_id: str) -> str:
    """
    Bloque listo para anteponer al prompt, o "" si el usuario no tiene hechos.
    Best-effort: cualquier fallo devuelve "" — el chat nunca se cae por esto.
    """
    if not user_id:
        return ""
    clave = f"{_CACHE_PREFIX}{_sanitize_user_id(user_id)}"

    try:
        cacheado = _redis().get(clave)
        if cacheado is not None:
            texto = cacheado.decode() if isinstance(cacheado, bytes) else str(cacheado)
            return "" if texto == _EMPTY_SENTINEL else texto
    except Exception as exc:
        logger.debug(f"[profile] caché no disponible: {exc}")

    try:
        hechos = _fetch_facts(user_id)
    except Exception as exc:
        # Incluye el 404 de colección inexistente (usuario sin memoria aún).
        logger.debug(f"[profile] sin hechos para {user_id}: {exc}")
        hechos = []

    bloque = _render(hechos)
    try:
        _redis().setex(clave, _CACHE_TTL_S, bloque or _EMPTY_SENTINEL)
    except Exception:
        pass
    return bloque


def _render(hechos: list[dict]) -> str:
    if not hechos:
        return ""
    # Preferencias primero: si el tope corta, que corte hechos, no preferencias.
    orden = sorted(
        hechos,
        key=lambda h: (h.get("fact_type") != "preference",
                       -float(h.get("importance", 0) or 0)),
    )
    lineas = [_HEADER]
    total = len(_HEADER)
    for h in orden:
        linea = f"- {str(h.get('text', '')).strip()}"
        if len(linea) <= 2:
            continue
        # Tope duro en código: se corta por LÍNEAS completas. Un hecho a medias
        # («prefiere respuestas lar…») es peor que un hecho menos.
        if total + len(linea) + 1 > MAX_BLOCK_CHARS:
            break
        lineas.append(linea)
        total += len(linea) + 1
    return "\n".join(lineas) if len(lineas) > 1 else ""


def invalidate(user_id: str) -> None:
    try:
        _redis().delete(f"{_CACHE_PREFIX}{_sanitize_user_id(user_id)}")
    except Exception:
        pass


def invalidate_for_collection(collection: str) -> None:
    """El consolidador conoce la colección, no el email — borra por sufijo."""
    sufijo = collection.removeprefix("memory_")
    try:
        _redis().delete(f"{_CACHE_PREFIX}{sufijo}")
    except Exception:
        pass
