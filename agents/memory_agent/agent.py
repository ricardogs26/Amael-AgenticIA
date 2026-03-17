"""
Zaphkiel — Agente de memoria episódica y contexto a largo plazo.

Angelología: Zaphkiel (צַפְקִיאֵל) es el ángel de la memoria y la contemplación.

Almacena episodios relevantes de conversaciones en Qdrant (colección por usuario),
los recupera como contexto enriquecido en futuras sesiones y permite el olvido
selectivo (GDPR).

Acciones disponibles:
  store    — indexa un episodio usuario/asistente si supera el umbral de importancia
  retrieve — busca memorias relevantes para una consulta
  forget   — elimina una memoria específica o todas (GDPR wipe)
  list     — lista memorias paginadas del usuario
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.zaphkiel")

_COLLECTION_PREFIX  = "memory_"
_VECTOR_SIZE        = 768   # nomic-embed-text
_IMPORTANCE_THRESHOLD = 0.3

# ── Regex para detectar preferencias y hechos personales ─────────────────────
_PREFERENCE_RE = re.compile(
    r"\b(prefiero|siempre|nunca|me gusta|no me gusta|odio|amo|i prefer|i always|"
    r"i never|i like|i don't like|mi proyecto|trabajo en|soy |me llamo|mi nombre|"
    r"usamos|decidimos|vamos a usar|elegimos|mi empresa|en mi trabajo|"
    r"my project|i work at|my name is)\b",
    re.IGNORECASE,
)


# ── Agent ─────────────────────────────────────────────────────────────────────

@AgentRegistry.register
class ZaphkielAgent(BaseAgent):
    """
    Zaphkiel — Memoria episódica por usuario.

    task dict por acción:

    store:
        {"action": "store", "user_id": str, "user_message": str,
         "assistant_reply": str, "conversation_id": str}

    retrieve:
        {"action": "retrieve", "user_id": str, "query": str, "k": int (default 5)}

    forget:
        {"action": "forget", "user_id": str, "id": str | None}
        # id=None → GDPR wipe (elimina toda la colección del usuario)

    list:
        {"action": "list", "user_id": str, "limit": int, "offset": int}
    """

    name         = "zaphkiel"
    role         = "Memoria episódica y contexto a largo plazo por usuario"
    version      = "1.0.0"
    capabilities = ["memory_store", "memory_retrieve", "memory_forget", "memory_list"]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        action = task.get("action", "retrieve")
        if action == "store":    return await self._store(task)
        if action == "retrieve": return await self._retrieve(task)
        if action == "forget":   return await self._forget(task)
        if action == "list":     return await self._list(task)
        return AgentResult(
            success=False, output=None, agent_name=self.name,
            error=f"Acción desconocida: '{action}'. Usa: store, retrieve, forget, list",
        )

    # ── store ─────────────────────────────────────────────────────────────────

    async def _store(self, task: Dict[str, Any]) -> AgentResult:
        user_id         = task.get("user_id", "").strip()
        user_message    = task.get("user_message", "").strip()
        assistant_reply = task.get("assistant_reply", "").strip()
        conversation_id = task.get("conversation_id") or str(uuid.uuid4())

        if not user_id or not user_message or not assistant_reply:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="user_id, user_message y assistant_reply son requeridos",
            )

        importance = _compute_importance(user_message, assistant_reply)
        if importance < _IMPORTANCE_THRESHOLD:
            return AgentResult(
                success=True,
                output={"stored": False, "reason": "importance below threshold",
                        "importance": round(importance, 3)},
                agent_name=self.name,
            )

        episode_type = _detect_episode_type(user_message)
        content      = (
            f"Usuario: {user_message[:400]}\n"
            f"Amael: {assistant_reply[:400]}"
        )

        try:
            client     = _get_qdrant_client()
            collection = _collection_name(user_id)
            await asyncio.to_thread(_ensure_collection, client, collection)

            vector   = await asyncio.to_thread(_embed_text, content)
            point_id = str(uuid.uuid4())

            from qdrant_client.models import PointStruct
            await asyncio.to_thread(
                client.upsert,
                collection_name=collection,
                points=[PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "episode_type":   episode_type,
                        "content":        content,
                        "user_message":   user_message[:600],
                        "assistant_reply": assistant_reply[:600],
                        "timestamp":      datetime.now(timezone.utc).isoformat(),
                        "importance":     round(importance, 3),
                        "conversation_id": conversation_id,
                    },
                )],
            )
            logger.debug(
                f"[zaphkiel] episodio almacenado: id={point_id[:8]} "
                f"user={user_id} importance={importance:.2f} type={episode_type}"
            )
            return AgentResult(
                success=True,
                output={"stored": True, "id": point_id,
                        "importance": round(importance, 3), "episode_type": episode_type},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.warning(f"[zaphkiel] store error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── retrieve ──────────────────────────────────────────────────────────────

    async def _retrieve(self, task: Dict[str, Any]) -> AgentResult:
        user_id = task.get("user_id", "").strip()
        query   = task.get("query", "").strip()
        k       = int(task.get("k", 5))

        if not user_id or not query:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="user_id y query son requeridos",
            )

        try:
            client     = _get_qdrant_client()
            collection = _collection_name(user_id)

            if not await asyncio.to_thread(_collection_exists, client, collection):
                return AgentResult(
                    success=True,
                    output={"context": "", "episodes": []},
                    agent_name=self.name,
                )

            vector  = await asyncio.to_thread(_embed_text, query)
            results = await asyncio.to_thread(
                lambda: client.query_points(
                    collection_name=collection,
                    query=vector,
                    limit=k,
                    with_payload=True,
                ).points
            )

            if not results:
                return AgentResult(
                    success=True,
                    output={"context": "", "episodes": []},
                    agent_name=self.name,
                )

            episodes      = [r.payload for r in results if r.payload]
            context_lines = []
            for ep in episodes:
                ts = ep.get("timestamp", "")[:10]
                context_lines.append(f"[{ts}] {ep.get('content', '')}")

            return AgentResult(
                success=True,
                output={"context": "\n".join(context_lines), "episodes": episodes},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.warning(f"[zaphkiel] retrieve error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── forget ────────────────────────────────────────────────────────────────

    async def _forget(self, task: Dict[str, Any]) -> AgentResult:
        user_id  = task.get("user_id", "").strip()
        point_id = task.get("id")   # None → GDPR wipe

        if not user_id:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="user_id es requerido",
            )

        try:
            client     = _get_qdrant_client()
            collection = _collection_name(user_id)

            if not await asyncio.to_thread(_collection_exists, client, collection):
                return AgentResult(
                    success=True, output={"deleted": 0}, agent_name=self.name,
                )

            if point_id:
                from qdrant_client.models import PointIdsList
                await asyncio.to_thread(
                    client.delete,
                    collection_name=collection,
                    points_selector=PointIdsList(points=[point_id]),
                )
                return AgentResult(
                    success=True,
                    output={"deleted": 1, "id": point_id},
                    agent_name=self.name,
                )
            else:
                # GDPR wipe — elimina la colección completa
                await asyncio.to_thread(client.delete_collection, collection)
                logger.info(f"[zaphkiel] GDPR wipe: colección {collection} eliminada")
                return AgentResult(
                    success=True,
                    output={"deleted": "all", "collection": collection},
                    agent_name=self.name,
                )
        except Exception as exc:
            logger.warning(f"[zaphkiel] forget error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))

    # ── list ──────────────────────────────────────────────────────────────────

    async def _list(self, task: Dict[str, Any]) -> AgentResult:
        user_id = task.get("user_id", "").strip()
        limit   = int(task.get("limit", 20))
        offset  = task.get("offset", None)   # Qdrant scroll offset token

        if not user_id:
            return AgentResult(
                success=False, output=None, agent_name=self.name,
                error="user_id es requerido",
            )

        try:
            client     = _get_qdrant_client()
            collection = _collection_name(user_id)

            if not await asyncio.to_thread(_collection_exists, client, collection):
                return AgentResult(
                    success=True,
                    output={"memories": [], "total": 0},
                    agent_name=self.name,
                )

            records, _next_offset = await asyncio.to_thread(
                lambda: client.scroll(
                    collection_name=collection,
                    limit=limit,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            )
            info  = await asyncio.to_thread(client.get_collection, collection)
            total = info.points_count

            memories = [
                {"id": str(r.id), **r.payload}
                for r in records if r.payload
            ]
            return AgentResult(
                success=True,
                output={"memories": memories, "total": total},
                agent_name=self.name,
            )
        except Exception as exc:
            logger.warning(f"[zaphkiel] list error: {exc}")
            return AgentResult(success=False, output=None, agent_name=self.name, error=str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _collection_name(user_id: str) -> str:
    sanitized = (
        user_id
        .replace("@", "_at_")
        .replace(".", "_dot_")
        .replace("+", "_plus_")
        .replace("-", "_dash_")
    )
    return f"{_COLLECTION_PREFIX}{sanitized}"


def _get_qdrant_client():
    from config.settings import settings
    from qdrant_client import QdrantClient
    return QdrantClient(url=settings.qdrant_url)


def _collection_exists(client, collection: str) -> bool:
    try:
        client.get_collection(collection)
        return True
    except Exception:
        return False


def _ensure_collection(client, collection: str) -> None:
    if _collection_exists(client, collection):
        return
    from qdrant_client.models import Distance, VectorParams
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=_VECTOR_SIZE, distance=Distance.COSINE),
    )
    logger.info(f"[zaphkiel] Colección creada: {collection}")


def _embed_text(text: str) -> List[float]:
    import requests as _req
    from config.settings import settings
    resp = _req.post(
        f"{settings.ollama_base_url}/api/embeddings",
        json={"model": settings.llm_embed_model, "prompt": text},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _compute_importance(user_msg: str, assistant_msg: str) -> float:
    """
    Heurística rápida (sin LLM) para decidir si un episodio merece ser almacenado.

    Score:
      0.3 — base (todo intercambio tiene valor mínimo)
      0.5 — mensajes largos (>200 chars): contienen más información
      0.55 — menciona errores/bugs/deploys: hechos técnicos relevantes
      0.7 — contiene marcadores de preferencia/identidad personal
    """
    combined = f"{user_msg} {assistant_msg}"
    score = 0.3
    if _PREFERENCE_RE.search(combined):
        score = max(score, 0.7)
    if len(user_msg) > 200:
        score = max(score, 0.5)
    if re.search(r"\b(error|bug|fix|deploy|crash|problema|fallo|incident)\b", combined, re.I):
        score = max(score, 0.55)
    return score


def _detect_episode_type(user_msg: str) -> str:
    if _PREFERENCE_RE.search(user_msg):
        return "preference"
    if re.search(r"\b(error|bug|fix|crash|problema|fallo)\b", user_msg, re.I):
        return "fact"
    return "conversation"
