"""
Router de documentos — GET /api/documents, DELETE /api/documents

Lista y elimina los documentos indexados del usuario autenticado.
Fuente primaria: PostgreSQL (user_documents).
Fuente de respaldo: Qdrant (chunks deduplicados por filename).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.routers.documents")

router = APIRouter(prefix="/api", tags=["documents"])


@router.get("/documents")
async def list_documents(user: str = Depends(get_current_user)) -> list[dict]:
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


def _from_postgres(user: str) -> list[dict] | None:
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


def _from_qdrant(user: str) -> list[dict]:
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


@router.delete("/documents")
async def delete_documents(user: str = Depends(get_current_user)) -> dict:
    """
    Elimina toda la colección de documentos del usuario autenticado.

    Borra la colección Qdrant del usuario y los registros en PostgreSQL.
    Operación irreversible — el usuario deberá re-ingestar sus documentos.

    Returns:
        { deleted: bool, message: str }
    """
    errors: list[str] = []

    # 1. Eliminar colección Qdrant
    try:
        from agents.researcher.rag_retriever import delete_user_collection
        delete_user_collection(user)
    except Exception as exc:
        logger.error(f"[documents] Qdrant delete failed for {user!r}: {exc}")
        errors.append(f"qdrant: {exc}")

    # 2. Eliminar registros en PostgreSQL
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_documents WHERE user_id = %s",
                    (user,),
                )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[documents] PostgreSQL delete failed for {user!r}: {exc}")
        errors.append(f"postgres: {exc}")

    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"Partial delete — some stores failed: {'; '.join(errors)}",
        )

    logger.info(f"[documents] All documents deleted for user={user!r}")
    return {"deleted": True, "message": "All documents removed from vector store and database."}
