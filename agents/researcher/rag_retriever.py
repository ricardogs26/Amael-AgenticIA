"""
RAG Retriever — búsqueda semántica sobre bases de conocimiento personales en Qdrant.

Migrado desde backend-ia/main.py:
  get_user_vectorstore()   — carga o crea la colección Qdrant por usuario
  rag_tool()               — similarity_search k=3 con métricas RAG_HITS/MISS

Cada usuario tiene su propia colección en Qdrant, nombrada con el email sanitizado.
El embedding se genera con nomic-embed-text vía OllamaEmbeddings.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("agents.researcher.rag")

_QDRANT_URL   = os.environ.get("QDRANT_URL",     "http://qdrant-service:6333")
_OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_EMBED_MODEL  = "nomic-embed-text"
_VECTOR_SIZE  = 768  # nomic-embed-text output dimension

# Module-level singletons (P5-3 optimization — avoid per-request reconnections)
# Los locks garantizan inicialización thread-safe bajo cargas concurrentes
_qdrant_client  = None
_embeddings     = None
_qdrant_lock    = threading.Lock()
_embeddings_lock = threading.Lock()


def _get_qdrant_client():
    global _qdrant_client
    if _qdrant_client is None:
        with _qdrant_lock:
            if _qdrant_client is None:  # double-checked locking
                from qdrant_client import QdrantClient
                _qdrant_client = QdrantClient(url=_QDRANT_URL)
    return _qdrant_client


def _get_embeddings():
    global _embeddings
    if _embeddings is None:
        with _embeddings_lock:
            if _embeddings is None:  # double-checked locking
                from langchain_ollama import OllamaEmbeddings
                _embeddings = OllamaEmbeddings(model=_EMBED_MODEL, base_url=_OLLAMA_URL)
    return _embeddings


def sanitize_email(email: str) -> str:
    """Convierte un email en nombre de colección Qdrant válido."""
    return email.replace("@", "_at_").replace(".", "_dot_")


def get_user_vectorstore(user_email: str):
    """
    Carga o crea la colección Qdrant para el usuario dado.

    Si la colección tiene un error de dimensiones (mismatch), la elimina y recrea.
    Migrado desde backend-ia/main.py → get_user_vectorstore()
    """
    from langchain_qdrant import QdrantVectorStore

    collection_name = sanitize_email(user_email)
    client    = _get_qdrant_client()
    embedding = _get_embeddings()

    try:
        vectorstore = QdrantVectorStore.from_existing_collection(
            embedding=embedding,
            collection_name=collection_name,
            url=_QDRANT_URL,
        )
        return vectorstore
    except Exception as exc:
        logger.warning(
            f"[rag] Error o dimensiones incorrectas en '{collection_name}': {exc}. "
            "Recreando colección."
        )

    # Si existe con error → eliminar y recrear
    if client.collection_exists(collection_name):
        logger.info(f"[rag] Borrando colección '{collection_name}' para corregir configuración.")
        client.delete_collection(collection_name)

    client.create_collection(
        collection_name=collection_name,
        vectors_config={"size": _VECTOR_SIZE, "distance": "Cosine"},
    )
    return QdrantVectorStore(
        client=client,
        collection_name=collection_name,
        embedding=embedding,
    )


def _extract_filename_from_source(source: str) -> str:
    """Extrae el filename limpio de un path de source (quita el UUID prefix /tmp/uuid-)."""
    import os
    basename = os.path.basename(source or "")
    # Archivos ingested: "<uuid>-<filename>" → tomar todo después del 5º guión (UUID = 4 bloques)
    parts = basename.split("-", 5)
    if len(parts) >= 6:
        return parts[5]
    return basename


_STOPWORDS = {
    "el", "la", "los", "las", "de", "del", "en", "un", "una", "y", "a",
    "que", "qué", "es", "se", "su", "al", "lo", "me", "te", "le", "no",
    "con", "por", "para", "sobre", "como", "más", "mi", "si", "ya",
    "hay", "he", "ha", "han", "pero", "o", "e", "ni", "sin", "muy",
    "dice", "dime", "dame", "what", "the", "of", "in", "and", "is",
    "pdf", "txt", "docx", "md", "documento", "archivo",
}


def _detect_filename_filter(query: str, user_email: str) -> str | None:
    """
    Detecta si el query hace referencia explícita a un filename de la colección.
    Umbral: 1 palabra significativa (>3 chars, no stopword) en común.
    Retorna la PALABRA que matcheó (para usar en MatchText), no el filename completo.
    """
    import re
    try:
        client = _get_qdrant_client()
        collection_name = sanitize_email(user_email)
        if not client.collection_exists(collection_name):
            return None

        points, _ = client.scroll(
            collection_name=collection_name,
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
        filenames: set[str] = set()
        for p in points:
            meta = (p.payload or {}).get("metadata", {})
            fn = meta.get("filename") or _extract_filename_from_source(meta.get("source", ""))
            if fn:
                filenames.add(fn.lower())

        # Limpiar query: quitar puntuación y stopwords
        query_words = {
            re.sub(r"[¿?¡!.,;:()\[\]\"']", "", w)
            for w in query.lower().split()
        }
        query_words = {w for w in query_words if len(w) > 3 and w not in _STOPWORDS}

        best_fn, best_score, best_keywords = None, 0, set()
        for fn in filenames:
            fn_words = set(re.split(r"[-_.\s]+", fn.lower()))
            fn_words = {w for w in fn_words if len(w) > 3 and w not in _STOPWORDS}
            common = query_words & fn_words
            if len(common) > best_score:
                best_score, best_fn, best_keywords = len(common), fn, common

        if best_score >= 1:
            # Devolver la primera keyword que matcheó — es la que usaremos en MatchText
            keyword = next(iter(best_keywords))
            logger.info(f"[rag] filename_filter: '{best_fn}' → keyword='{keyword}'")
            return keyword
        return None
    except Exception as exc:
        logger.debug(f"[rag] _detect_filename_filter ignorado: {exc}")
        return None


_RAG_CACHE_TTL = 300  # 5 minutos


def _rag_cache_key(user_email: str, query: str, filename_filter: str | None) -> str:
    import hashlib
    raw = f"{user_email}:{query}:{filename_filter or ''}"
    return "rag_cache:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def retrieve_documents(
    user_email: str,
    query: str,
    k: int = 5,
    filename_filter: str | None = None,
) -> str:
    """
    Recupera los k chunks más relevantes para el query del usuario.

    Si filename_filter está presente (o se detecta uno en el query),
    filtra en Qdrant por el campo source/filename del metadata.

    Retorna texto con cabeceras de fuente para que el LLM cite correctamente.
    """
    import time

    # Cache en Redis — evita re-embeddear el mismo query en la misma sesión
    try:
        from storage.redis.client import get_redis_client
        _rc = get_redis_client()
        _cache_key = _rag_cache_key(user_email, query, filename_filter)
        _cached = _rc.get(_cache_key)
        if _cached:
            logger.debug(f"[rag] cache hit: {_cache_key[:20]}...")
            return _cached.decode() if isinstance(_cached, bytes) else _cached
    except Exception:
        _rc = None
        _cache_key = None

    from observability.metrics import (
        RAG_DOCS_RETURNED,
        RAG_FILTER_APPLIED_TOTAL,
        RAG_HITS_TOTAL,
        RAG_LATENCY_SECONDS,
        RAG_MISS_TOTAL,
        RAG_RERANK_LATENCY_SECONDS,
    )

    _t0 = time.monotonic()
    try:
        effective_filter = filename_filter or _detect_filename_filter(query, user_email)
        vectorstore = get_user_vectorstore(user_email)

        if effective_filter:
            # Estrategia: scroll SIN filtro Qdrant + substring filter en Python.
            # MatchText requiere índice FTS y es case-sensitive sin él.
            RAG_FILTER_APPLIED_TOTAL.labels(filter_type="filename").inc()
            keyword = effective_filter.lower()
            collection_name = sanitize_email(user_email)
            client = _get_qdrant_client()

            all_points, _ = client.scroll(
                collection_name=collection_name,
                limit=500,
                with_payload=True,
                with_vectors=False,
            )
            from langchain_core.documents import Document as LCDocument
            matched = [
                LCDocument(
                    page_content=p.payload.get("page_content", ""),
                    metadata=p.payload.get("metadata", {}),
                )
                for p in all_points
                if keyword in (p.payload.get("metadata", {}).get("filename") or "").lower()
                or keyword in (p.payload.get("metadata", {}).get("source") or "").lower()
            ]
            logger.info(f"[rag] filtro '{keyword}': {len(matched)}/{len(all_points)} chunks")

            if matched:
                # Reranking semántico en memoria — comparar embeddings
                _tr0 = time.monotonic()
                embeddings = _get_embeddings()
                query_vec = embeddings.embed_query(query)
                import numpy as np

                def _cos_sim(a, b):
                    a, b = np.array(a), np.array(b)
                    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))

                chunk_texts = [d.page_content for d in matched]
                chunk_vecs  = embeddings.embed_documents(chunk_texts)
                scored = sorted(
                    zip(matched, chunk_vecs),
                    key=lambda x: _cos_sim(query_vec, x[1]),
                    reverse=True,
                )
                docs = [d for d, _ in scored[:k]]
                RAG_RERANK_LATENCY_SECONDS.observe(time.monotonic() - _tr0)
            else:
                logger.warning(f"[rag] filtro '{keyword}' sin resultados — búsqueda global")
                docs = vectorstore.similarity_search(query, k=k)
        else:
            RAG_FILTER_APPLIED_TOTAL.labels(filter_type="global").inc()
            docs = vectorstore.similarity_search(query, k=k)
            logger.debug(f"[rag] global: {len(docs)} chunks user={user_email!r}")

        RAG_DOCS_RETURNED.observe(len(docs))

        if not docs:
            RAG_MISS_TOTAL.inc()
            RAG_LATENCY_SECONDS.observe(time.monotonic() - _t0)
            return ""

        RAG_HITS_TOTAL.inc()
        parts = []
        for doc in docs:
            src = doc.metadata.get("filename") or _extract_filename_from_source(
                doc.metadata.get("source", "")
            )
            page = doc.metadata.get("page", "")
            header = f"[Fuente: {src}, pág. {page}]" if page != "" else f"[Fuente: {src}]"
            parts.append(f"{header}\n{doc.page_content}")
        RAG_LATENCY_SECONDS.observe(time.monotonic() - _t0)
        result_text = "\n\n".join(parts)
        try:
            if _rc and _cache_key:
                _rc.setex(_cache_key, _RAG_CACHE_TTL, result_text)
        except Exception:
            pass
        return result_text

    except Exception as exc:
        logger.error(f"[rag] retrieve_documents error: {exc}")
        RAG_MISS_TOTAL.inc()
        RAG_LATENCY_SECONDS.observe(time.monotonic() - _t0)
        return ""


def ingest_document(
    user_email: str,
    text_chunks: list[str],
    metadata: dict | None = None,
) -> int:
    """
    Indexa chunks de texto en la colección del usuario.

    Args:
        user_email:   Email del propietario de los documentos.
        text_chunks:  Lista de fragmentos de texto ya divididos.
        metadata:     Metadata opcional aplicada a todos los chunks.

    Returns:
        Número de chunks indexados.
    """
    from langchain.schema import Document

    if not text_chunks:
        return 0

    vectorstore = get_user_vectorstore(user_email)
    docs = [
        Document(page_content=chunk, metadata=metadata or {})
        for chunk in text_chunks
    ]
    vectorstore.add_documents(docs)
    logger.info(f"[rag] {len(docs)} chunks indexados para user={user_email!r}")
    return len(docs)


def list_user_documents(user_email: str, limit: int = 20) -> list[dict]:
    """
    Lista los documentos indexados del usuario (por payload Qdrant).
    Útil para el endpoint GET /api/documents.
    """
    try:
        client = _get_qdrant_client()
        collection_name = sanitize_email(user_email)
        if not client.collection_exists(collection_name):
            return []
        points, _ = client.scroll(
            collection_name=collection_name,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            {
                "id": str(p.id),
                "metadata": p.payload or {},
                "preview": (p.payload or {}).get("page_content", "")[:200],
            }
            for p in points
        ]
    except Exception as exc:
        logger.error(f"[rag] list_user_documents error: {exc}")
        return []


def delete_user_collection(user_email: str) -> bool:
    """Elimina toda la colección del usuario de Qdrant."""
    try:
        client = _get_qdrant_client()
        collection_name = sanitize_email(user_email)
        if client.collection_exists(collection_name):
            client.delete_collection(collection_name)
            logger.info(f"[rag] Colección '{collection_name}' eliminada.")
            return True
        return False
    except Exception as exc:
        logger.error(f"[rag] delete_user_collection error: {exc}")
        return False
