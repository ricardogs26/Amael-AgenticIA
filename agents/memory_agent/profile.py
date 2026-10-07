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

7-oct-2026 — relevancia por pregunta. Inyectar TODO en cada turno hizo que un
«hola, ¿cómo estás?» por voz se respondiera con «…y plantas tus vegetales este
domingo» (y el bloque lleva datos de salud y familia a cualquier respuesta). El
«no lo cites» del encabezado no bastó. Ahora: las PREFERENCIAS van siempre (la
lección de Hermes sigue en pie); los demás hechos solo si comparten una raíz
significativa con la pregunta. Determinista, en código. Sin pregunta (day
planner) van todos, como antes. La caché guarda los hechos, no el bloque, para
filtrar por pregunta sin repetir el scroll.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata

logger = logging.getLogger("agents.memory.profile")

_QDRANT_URL   = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
MAX_BLOCK_CHARS = int(os.environ.get("MEMORY_PROFILE_MAX_CHARS", "900"))
_CACHE_TTL_S    = int(os.environ.get("MEMORY_PROFILE_CACHE_TTL_S", "3600"))
_CACHE_PREFIX   = "profile_facts:"     # JSON de hechos (antes «profile_block:» con el texto)
_OLD_PREFIX     = "profile_block:"
# Centinela para cachear también el caso «sin hechos»: sin esto, cada mensaje
# de un usuario sin perfil pagaría el scroll a Qdrant completo.
_EMPTY_SENTINEL = "\x00empty"

_HEADER = "Contexto del usuario (no lo cites ni menciones; úsalo solo si es relevante):"

# Palabras que no prueban relevancia: estructura y verbos que aparecen en casi
# cualquier pregunta Y en casi cualquier hecho («hace», «tiene»…).
_STOPWORDS = frozenset("""
como cual cuales cuando cuanto donde quien quienes porque para pero sobre desde
hasta entre este esta estos estas ese esos esas aquel algo nada todo todos todas
muy mas menos tambien solo ahora luego antes despues siempre nunca bien hola
buenos buenas dias tardes noches gracias favor puedes puede pueden podria quiero
quieres necesito tengo tiene tienen tienes tener hace hacer hago haces hacen
estoy estas esta estan estar eres somos fueron sera seria hay habia haber dime
decir dijo saber sabes ver mira cosa cosas mismo otra otro otras otros cada
""".split())
_WORD_RE = re.compile(r"[a-z0-9]+")


def _roots(texto: str) -> set[str]:
    """Raíces significativas: sin acentos, minúsculas, ≥4 letras, sin stopwords,
    primeras 5 letras (plantar ≈ plantas ≈ planta)."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return {w[:5] for w in _WORD_RE.findall(t) if len(w) >= 4 and w not in _STOPWORDS}


def _relevantes(hechos: list[dict], pregunta: str | None) -> list[dict]:
    """Preferencias siempre; hechos solo si comparten raíz con la pregunta."""
    if pregunta is None:
        return hechos
    q = _roots(pregunta)
    return [
        h for h in hechos
        if h.get("fact_type") == "preference" or (q & _roots(str(h.get("text", ""))))
    ]


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


def render_profile_block(user_id: str, question: str | None = None) -> str:
    """
    Bloque listo para anteponer al prompt, o "" si no hay nada que inyectar.
    `question`: filtra los hechos no-preferencia por relevancia (ver docstring
    del módulo); None = todos (day planner).
    Best-effort: cualquier fallo devuelve "" — el chat nunca se cae por esto.
    """
    if not user_id:
        return ""
    return _render(_relevantes(_cached_facts(user_id), question))


def _cached_facts(user_id: str) -> list[dict]:
    clave = f"{_CACHE_PREFIX}{_sanitize_user_id(user_id)}"
    try:
        cacheado = _redis().get(clave)
        if cacheado is not None:
            texto = cacheado.decode() if isinstance(cacheado, bytes) else str(cacheado)
            return [] if texto == _EMPTY_SENTINEL else json.loads(texto)
    except Exception as exc:
        logger.debug(f"[profile] caché no disponible: {exc}")

    try:
        hechos = _fetch_facts(user_id)
    except Exception as exc:
        # Incluye el 404 de colección inexistente (usuario sin memoria aún).
        logger.debug(f"[profile] sin hechos para {user_id}: {exc}")
        hechos = []

    # Solo lo que _render usa: texto, tipo e importancia.
    hechos = [
        {"text": h.get("text", ""), "fact_type": h.get("fact_type"),
         "importance": h.get("importance", 0)}
        for h in hechos
    ]
    try:
        _redis().setex(clave, _CACHE_TTL_S,
                       json.dumps(hechos, ensure_ascii=False) if hechos else _EMPTY_SENTINEL)
    except Exception:
        pass
    return hechos


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
        _redis().delete(f"{_OLD_PREFIX}{_sanitize_user_id(user_id)}")
    except Exception:
        pass


def invalidate_for_collection(collection: str) -> None:
    """El consolidador conoce la colección, no el email — borra por sufijo."""
    sufijo = collection.removeprefix("memory_")
    try:
        _redis().delete(f"{_CACHE_PREFIX}{sufijo}")
        _redis().delete(f"{_OLD_PREFIX}{sufijo}")
    except Exception:
        pass
