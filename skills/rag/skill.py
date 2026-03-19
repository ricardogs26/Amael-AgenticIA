"""
RAGSkill — recuperación y gestión de documentos en Qdrant (per-user).

Capacidades:
  retrieve(user_email, query, k)        — similarity_search top-k
  ingest(user_email, chunks, metadata)  — indexa chunks en la colección del usuario
  list_docs(user_email, limit)          — lista documentos indexados
  delete_collection(user_email)         — elimina toda la colección del usuario

Wrapper sobre agents/researcher/rag_retriever.
"""
from __future__ import annotations

import logging
from typing import Any

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.rag")


# ── Inputs ────────────────────────────────────────────────────────────────────

class RetrieveInput(SkillInput):
    user_email: str
    query: str
    k: int = 3

class IngestInput(SkillInput):
    user_email: str
    chunks: list[str]
    metadata: dict[str, Any] = {}

class ListDocsInput(SkillInput):
    user_email: str
    limit: int = 20

class DeleteCollectionInput(SkillInput):
    user_email: str


# ── Skill ─────────────────────────────────────────────────────────────────────

class RAGSkill(BaseSkill):
    """
    Capacidad de RAG sobre documentos personales del usuario.
    Cada usuario tiene su propia colección en Qdrant nombrada con su email sanitizado.
    """

    name        = "rag"
    description = "RAG sobre documentos del usuario: retrieve, ingest, list, delete"
    version     = "1.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        if isinstance(input, RetrieveInput):
            return await self.retrieve(input)
        if isinstance(input, IngestInput):
            return await self.ingest(input)
        if isinstance(input, ListDocsInput):
            return await self.list_docs(input)
        if isinstance(input, DeleteCollectionInput):
            return await self.delete_collection(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por RAGSkill")

    async def retrieve(self, input: RetrieveInput) -> SkillOutput:
        """Recupera los documentos más relevantes para el query del usuario."""
        try:
            from agents.researcher.rag_retriever import retrieve_documents
            content = retrieve_documents(input.user_email, input.query, k=input.k)
            if not content:
                return SkillOutput.ok(
                    data="",
                    hits=0,
                    user=input.user_email,
                )
            return SkillOutput.ok(
                data=content,
                hits=1,
                user=input.user_email,
                query=input.query,
            )
        except Exception as exc:
            logger.error(f"[rag_skill] retrieve error: {exc}")
            return SkillOutput.fail(str(exc))

    async def ingest(self, input: IngestInput) -> SkillOutput:
        """Indexa chunks de texto en la colección Qdrant del usuario."""
        try:
            from agents.researcher.rag_retriever import ingest_document
            count = ingest_document(
                input.user_email,
                input.chunks,
                metadata=input.metadata,
            )
            return SkillOutput.ok(
                data={"indexed": count},
                user=input.user_email,
                chunks=count,
            )
        except Exception as exc:
            logger.error(f"[rag_skill] ingest error: {exc}")
            return SkillOutput.fail(str(exc))

    async def list_docs(self, input: ListDocsInput) -> SkillOutput:
        """Lista los documentos indexados del usuario."""
        try:
            from agents.researcher.rag_retriever import list_user_documents
            docs = list_user_documents(input.user_email, limit=input.limit)
            return SkillOutput.ok(data=docs, count=len(docs), user=input.user_email)
        except Exception as exc:
            logger.error(f"[rag_skill] list_docs error: {exc}")
            return SkillOutput.fail(str(exc))

    async def delete_collection(self, input: DeleteCollectionInput) -> SkillOutput:
        """Elimina toda la colección Qdrant del usuario."""
        try:
            from agents.researcher.rag_retriever import delete_user_collection
            deleted = delete_user_collection(input.user_email)
            return SkillOutput.ok(
                data={"deleted": deleted},
                user=input.user_email,
            )
        except Exception as exc:
            logger.error(f"[rag_skill] delete_collection error: {exc}")
            return SkillOutput.fail(str(exc))

    async def health_check(self) -> bool:
        """Verifica que Qdrant responde."""
        try:
            from agents.researcher.rag_retriever import _get_qdrant_client
            _get_qdrant_client().get_collections()
            return True
        except Exception as exc:
            logger.warning(f"[rag_skill] health_check falló: {exc}")
            return False
