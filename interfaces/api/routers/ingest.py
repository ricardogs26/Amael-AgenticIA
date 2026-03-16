"""
Router de ingesta de documentos — POST /api/ingest

Flujo:
  1. Leer y detectar MIME (PDF / TXT / DOCX)
  2. Extraer texto
  3. Chunking + indexar en Qdrant (colección por usuario)
  4. Generar resumen con LLM
  5. Guardar metadata en PostgreSQL (user_documents)
  6. Backup en MinIO (best-effort)

Migrado desde backend-ia/main.py → ingest_data().
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from interfaces.api.auth import get_current_user

logger = logging.getLogger("interfaces.api.routers.ingest")

router = APIRouter(prefix="/api", tags=["ingest"])

_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5:14b")


def _extract_docx_text(content: bytes) -> str:
    """Extrae texto de un archivo DOCX."""
    import io
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        logger.warning(f"[ingest] Error extrayendo DOCX: {e}")
        return ""


def _sanitize_email(email: str) -> str:
    """Convierte email en nombre válido para bucket MinIO."""
    return email.replace("@", "_at_").replace(".", "_")


@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(...),
    user: str = Depends(get_current_user),
):
    """
    Sube y procesa un documento (PDF, TXT, DOCX, MD).
    Lo indexa en Qdrant y guarda metadata en PostgreSQL.

    Returns:
        { doc_id, filename, summary, chunks }
    """
    temp_path: Optional[str] = None
    content = await file.read()

    # ── 1. Detectar MIME ──────────────────────────────────────────────────────
    try:
        import magic
        mime = magic.from_buffer(content, mime=True)
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo determinar el tipo de archivo.")

    fname = (file.filename or "").lower()
    if fname.endswith(".docx"):
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if fname.endswith(".md"):
        mime = "text/plain"

    # ── 2. Extraer texto ──────────────────────────────────────────────────────
    from langchain_core.documents import Document as LCDocument

    full_text = ""
    documents: list[LCDocument] = []

    if mime == "application/pdf":
        temp_path = f"/tmp/{uuid.uuid4()}-{file.filename}"
        with open(temp_path, "wb") as buf:
            buf.write(content)
        try:
            from langchain_community.document_loaders import PyPDFLoader
            loader = PyPDFLoader(temp_path)
            documents = loader.load()
            full_text = "\n".join(d.page_content for d in documents)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error leyendo PDF: {e}")

    elif mime == "text/plain":
        full_text = content.decode("utf-8", errors="replace")
        documents = [LCDocument(page_content=full_text)]

    elif "wordprocessingml" in mime or fname.endswith(".docx"):
        full_text = _extract_docx_text(content)
        documents = [LCDocument(page_content=full_text)]

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no soportado: '{mime}'. Usa PDF, TXT, DOCX o MD.",
        )

    if not full_text.strip():
        raise HTTPException(status_code=422, detail="El documento está vacío o no se pudo extraer texto.")

    # ── 3. Chunking + indexar en Qdrant ───────────────────────────────────────
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from agents.researcher.rag_retriever import get_user_vectorstore
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        chunks = splitter.split_documents(documents)
        # Asegurar que cada chunk lleva el filename limpio en metadata
        for chunk in chunks:
            chunk.metadata["filename"] = file.filename
        vs = get_user_vectorstore(user)
        vs.add_documents(chunks)
        n_chunks = len(chunks)
        logger.info(f"[ingest] {n_chunks} chunks indexados en Qdrant para '{user}'")
    except Exception as e:
        logger.error(f"[ingest] Error indexando en Qdrant: {e}")
        raise HTTPException(status_code=500, detail=f"Error indexando documento: {e}")

    # ── 4. Generar resumen con LLM ────────────────────────────────────────────
    summary = ""
    try:
        from langchain_ollama import OllamaLLM
        llm = OllamaLLM(model=_MODEL_NAME, base_url=_OLLAMA_URL)
        summary = llm.invoke(
            f"Resume el siguiente documento en 2-3 oraciones en español:\n\n{full_text[:3000]}"
        ).strip()
    except Exception as e:
        summary = f"Documento procesado ({n_chunks} fragmentos)."
        logger.warning(f"[ingest] Error generando resumen: {e}")

    # ── 5. Metadata en PostgreSQL ─────────────────────────────────────────────
    doc_id = None
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_documents (user_id, doc_type, summary, raw_analysis) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (user, file.filename, summary, full_text[:10000]),
                )
                doc_id = cur.fetchone()[0]
            conn.commit()
    except Exception as e:
        logger.warning(f"[ingest] No se guardó metadata en PostgreSQL: {e}")

    # ── 6. Backup en MinIO (best-effort) ─────────────────────────────────────
    try:
        import io
        from storage.minio.client import get_client
        minio = get_client()
        bucket = _sanitize_email(user).replace("_", "-")[:63]
        if not minio.bucket_exists(bucket):
            minio.make_bucket(bucket)
        file_data = content if temp_path is None else open(temp_path, "rb").read()
        minio.put_object(
            bucket,
            file.filename,
            io.BytesIO(file_data),
            length=len(file_data),
            content_type=mime,
        )
        logger.info(f"[ingest] Backup MinIO: {bucket}/{file.filename}")
    except Exception as e:
        logger.warning(f"[ingest] MinIO backup falló (no crítico): {e}")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if temp_path and os.path.exists(temp_path):
        os.remove(temp_path)

    return {
        "doc_id":   doc_id,
        "filename": file.filename,
        "summary":  summary,
        "chunks":   n_chunks,
    }
