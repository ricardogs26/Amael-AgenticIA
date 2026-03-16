"""
Router de documentos — GET /api/documents

Lista los documentos indexados del usuario autenticado.
Fuente primaria: PostgreSQL (user_documents).
Fuente de respaldo: Qdrant (chunks deduplicados por filename).
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.routers.documents")

router = APIRouter(prefix="/api", tags=["documents"])


@router.get("/documents")
async def list_documents(user: str = Depends(get_current_user)) -> List[dict]:
    """
    Retorna la lista de documentos indexados para el usuario autenticado.

    Intenta leer desde PostgreSQL (user_documents) primero.
    Si falla, genera la lista desde los payloads de Qdrant.

    Returns:
        Lista de { id, filename, summary, uploaded_at }
    """
    docs = _from_postgres(user)
    if docs is not None:
        return docs
    return _from_qdrant(user)


def _from_postgres(user: str) -> List[dict] | None:
    """Consulta user_documents en PostgreSQL."""
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, doc_type, summary, created_at
                    FROM user_documents
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (user,),
                )
                rows = cur.fetchall()
        return [
            {
                "id":          str(row[0]),
                "filename":    row[1] or "",
                "summary":     row[2] or "",
                "uploaded_at": row[3].isoformat() if row[3] else None,
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning(f"[documents] PostgreSQL query failed, falling back to Qdrant: {exc}")
        return None


def _from_qdrant(user: str) -> List[dict]:
    """Genera lista de documentos desde payloads de Qdrant (deduplicado por filename)."""
    try:
        from agents.researcher.rag_retriever import list_user_documents
        chunks = list_user_documents(user, limit=500)
        seen: dict[str, dict] = {}
        for chunk in chunks:
            meta = (chunk.get("metadata") or {}).get("metadata") or chunk.get("metadata") or {}
            filename = meta.get("filename") or ""
            if not filename or filename in seen:
                continue
            seen[filename] = {
                "id":          chunk.get("id", ""),
                "filename":    filename,
                "summary":     "",
                "uploaded_at": None,
            }
        return list(seen.values())
    except Exception as exc:
        logger.error(f"[documents] Qdrant fallback failed: {exc}")
        return []
