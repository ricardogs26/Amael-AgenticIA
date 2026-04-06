"""
SRE Runbook Consolidator — Nivel 3.

Consolida múltiples auto-runbooks del mismo issue_type en un único runbook
enriquecido con patrones aprendidos (frecuencia, causas comunes, correlaciones).

Ejecutado por APScheduler cada 24h (03:00 UTC). Solo actúa cuando hay ≥3
runbooks auto_generated para el mismo issue_type — cantidad mínima para
extraer patrones significativos.

Flujo:
  1. Escanea Qdrant: agrupa auto_generated por issue_type
  2. Para cada issue_type con ≥ MIN_RUNBOOKS_TO_CONSOLIDATE:
       a. LLM sintetiza los N runbooks en uno enriquecido
       b. Upsert del runbook consolidado (consolidated=True)
       c. Elimina los N runbooks individuales de Qdrant
  3. Registra métrica amael_sre_runbook_consolidated_total
"""
from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger("agents.sre.consolidator")

_QDRANT_URL              = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
_SRE_RUNBOOKS_COLLECTION = "sre_runbooks"
_OLLAMA_BASE_URL         = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_EMBED_MODEL             = "nomic-embed-text"
MIN_RUNBOOKS_TO_CONSOLIDATE = 3   # mínimo de auto-runbooks para disparar consolidación
MAX_RUNBOOKS_PER_TYPE       = 10  # máximo a consolidar por issue_type (evitar prompts gigantes)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_embedding(text: str) -> list[float] | None:
    try:
        from agents.base.llm_factory import get_embeddings
        return get_embeddings().embed_query(text)
    except Exception as exc:
        logger.warning(f"[consolidator] Embedding falló: {exc}")
        return None


def _get_llm():
    from agents.base.llm_factory import get_chat_llm
    return get_chat_llm()


# ── Scan Qdrant ───────────────────────────────────────────────────────────────

def _fetch_auto_runbooks_by_type() -> dict[str, list[dict]]:
    """
    Lee todos los puntos auto_generated=True de Qdrant y los agrupa por issue_type.
    Retorna {issue_type: [{"id": ..., "text": ..., "resource_name": ..., ...}]}
    """
    groups: dict[str, list[dict]] = {}
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = QdrantClient(url=_QDRANT_URL)
        offset = None

        while True:
            result, next_offset = client.scroll(
                collection_name=_SRE_RUNBOOKS_COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="auto_generated", match=MatchValue(value=True)),
                    # Solo auto-runbooks de incidentes — no los de bootstrap
                    FieldCondition(key="bootstrapped",   match=MatchValue(value=False)),
                ]),
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in result:
                p = point.payload or {}
                issue_type = p.get("issue_type", "UNKNOWN")
                # Skip si ya es consolidado (evitar re-consolidar)
                if p.get("consolidated"):
                    continue
                groups.setdefault(issue_type, []).append({
                    "id":            str(point.id),
                    "text":          p.get("text", ""),
                    "resource_name": p.get("resource_name", ""),
                    "namespace":     p.get("namespace", ""),
                    "action_taken":  p.get("action_taken", ""),
                    "timestamp":     p.get("timestamp", ""),
                })

            if next_offset is None:
                break
            offset = next_offset

    except Exception as exc:
        logger.warning(f"[consolidator] Error leyendo Qdrant: {exc}")

    return groups


# ── LLM synthesis ─────────────────────────────────────────────────────────────

def _synthesize_runbooks(issue_type: str, runbooks: list[dict]) -> str:
    """
    El LLM sintetiza N runbooks del mismo issue_type en un documento consolidado
    con patrones aprendidos. Timeout 120s. Retorna "" si falla.
    """
    # Construir resumen de incidentes para el prompt
    incidents_summary = ""
    for i, rb in enumerate(runbooks[:MAX_RUNBOOKS_PER_TYPE], 1):
        incidents_summary += (
            f"\n--- Incidente {i} ---\n"
            f"Recurso: {rb['namespace']}/{rb['resource_name']}\n"
            f"Acción: {rb['action_taken']}\n"
            f"Fecha: {rb['timestamp']}\n"
            f"{rb['text'][:600]}\n"
        )

    prompt = (
        f"Eres un SRE experto. Analiza estos {len(runbooks)} incidentes del tipo "
        f"`{issue_type}` que ocurrieron en producción y fueron resueltos.\n"
        f"{incidents_summary}\n\n"
        "Genera un RUNBOOK CONSOLIDADO en Markdown con patrones aprendidos. "
        "Usa EXACTAMENTE estas secciones (muy conciso, máx 5 bullets cada una):\n\n"
        f"# Runbook consolidado: {issue_type}\n"
        f"*Basado en {len(runbooks)} incidentes resueltos*\n\n"
        "## Patrón observado\n"
        "Qué tienen en común estos incidentes (recursos afectados, frecuencia, contexto).\n\n"
        "## Causas raíz más frecuentes\n"
        "Lista ordenada por frecuencia de aparición.\n\n"
        "## Síntomas y detección temprana\n"
        "Señales observables antes del fallo, con comandos kubectl específicos.\n\n"
        "## Remediación probada\n"
        "Pasos que funcionaron en estos incidentes específicos.\n\n"
        "## Prevención\n"
        "Cambios de configuración concretos para evitar recurrencia.\n"
    )

    try:
        import concurrent.futures
        from agents.base.llm_factory import llm_agent_context
        llm = _get_llm()
        llm_agent_context.set("runbook_l3")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(llm.invoke, prompt)
        try:
            raw = future.result(timeout=120)
        finally:
            executor.shutdown(wait=False)
        if hasattr(raw, "content"):
            raw = raw.content
        return raw.strip()
    except Exception as exc:
        logger.warning(f"[consolidator] LLM síntesis falló para {issue_type}: {type(exc).__name__}: {exc}")
        return ""


# ── Qdrant write/delete ───────────────────────────────────────────────────────

def _save_consolidated_runbook(issue_type: str, text: str, source_count: int) -> bool:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct

        embedding = _get_embedding(text)
        if not embedding:
            return False

        client = QdrantClient(url=_QDRANT_URL)
        point = PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text":              text,
                "issue_type":        issue_type,
                "auto_generated":    True,
                "bootstrapped":      False,
                "consolidated":      True,
                "consolidated_from": source_count,
            },
        )
        client.upsert(collection_name=_SRE_RUNBOOKS_COLLECTION, points=[point])
        return True
    except Exception as exc:
        logger.warning(f"[consolidator] Error guardando consolidado: {exc}")
        return False


def _delete_runbook_points(point_ids: list[str]) -> None:
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointIdsList

        client = QdrantClient(url=_QDRANT_URL)
        client.delete(
            collection_name=_SRE_RUNBOOKS_COLLECTION,
            points_selector=PointIdsList(points=point_ids),
        )
    except Exception as exc:
        logger.warning(f"[consolidator] Error borrando puntos: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_consolidation() -> None:
    """
    Job principal de consolidación. Llamado por APScheduler cada 24h.

    Escanea Qdrant, agrupa auto-runbooks por issue_type, consolida los que
    tienen ≥ MIN_RUNBOOKS_TO_CONSOLIDATE entradas y limpia las individuales.
    """
    try:
        from observability.metrics import SRE_AUTO_RUNBOOK_SAVED_TOTAL
    except Exception:
        SRE_AUTO_RUNBOOK_SAVED_TOTAL = None

    logger.info("[consolidator] Iniciando consolidación de runbooks...")

    groups = _fetch_auto_runbooks_by_type()
    if not groups:
        logger.info("[consolidator] No hay runbooks auto-generados en Qdrant.")
        return

    consolidated_count = 0

    for issue_type, runbooks in groups.items():
        if len(runbooks) < MIN_RUNBOOKS_TO_CONSOLIDATE:
            logger.debug(
                f"[consolidator] {issue_type}: {len(runbooks)} runbooks "
                f"(< {MIN_RUNBOOKS_TO_CONSOLIDATE}). Skipping."
            )
            continue

        logger.info(
            f"[consolidator] Consolidando {len(runbooks)} runbooks de {issue_type}..."
        )

        consolidated_text = _synthesize_runbooks(issue_type, runbooks)
        if not consolidated_text:
            logger.warning(f"[consolidator] LLM falló para {issue_type}. Saltando.")
            continue

        saved = _save_consolidated_runbook(
            issue_type=issue_type,
            text=consolidated_text,
            source_count=len(runbooks),
        )
        if not saved:
            continue

        # Borrar los runbooks individuales que ya fueron consolidados
        ids_to_delete = [rb["id"] for rb in runbooks[:MAX_RUNBOOKS_PER_TYPE]]
        _delete_runbook_points(ids_to_delete)

        if SRE_AUTO_RUNBOOK_SAVED_TOTAL:
            SRE_AUTO_RUNBOOK_SAVED_TOTAL.inc()

        consolidated_count += 1
        logger.info(
            f"[consolidator] ✅ {issue_type}: {len(runbooks)} runbooks → 1 consolidado "
            f"(borrados {len(ids_to_delete)} individuales)"
        )

    logger.info(
        f"[consolidator] Consolidación completada: {consolidated_count} tipos procesados."
    )
