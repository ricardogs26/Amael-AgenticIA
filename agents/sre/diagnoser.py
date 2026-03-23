"""
SRE Diagnoser — diagnóstico de anomalías con LLM + RAG de runbooks.

Migrado desde k8s-agent/main.py:
  diagnose_with_llm()           — diagnóstico LLM con timeout 30s (P2)
  adjust_confidence_with_history() — blending histórico 70/30 (P3-B)
  search_runbooks()             — RAG sobre runbooks en Qdrant (P2)
  _maybe_save_runbook_entry()   — auto-runbooks en Qdrant (P4-D)
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("agents.sre.diagnoser")

_QDRANT_URL              = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
_SRE_RUNBOOKS_COLLECTION = "sre_runbooks"

# ── Singleton LLM para diagnóstico ────────────────────────────────────────────
_diag_llm = None


def _get_diag_llm():
    global _diag_llm
    if _diag_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _diag_llm = get_chat_llm()
    return _diag_llm


def _get_embedding(text: str) -> list[float] | None:
    """Genera embedding via el provider configurado."""
    try:
        from agents.base.llm_factory import get_embeddings
        return get_embeddings().embed_query(text)
    except Exception as exc:
        logger.warning(f"[diagnoser] Embedding falló: {exc}")
        return None


def search_runbooks(issue_type: str, details: str) -> str:
    """
    Busca runbooks relevantes en Qdrant para el issue_type dado.
    Usa query_points() (Qdrant v1.7+) con fallback a search() (P2).

    Migrado desde k8s-agent/main.py → search_runbooks()
    """
    try:
        from qdrant_client import QdrantClient

        query_text = f"{issue_type}: {details}"
        embedding  = _get_embedding(query_text)
        if not embedding:
            return ""

        client = QdrantClient(url=_QDRANT_URL)

        try:
            # Qdrant v1.7+ — query_points()
            results = client.query_points(
                collection_name=_SRE_RUNBOOKS_COLLECTION,
                query=embedding,
                limit=3,
            ).points
        except AttributeError:
            # Fallback para versiones anteriores
            results = client.search(
                collection_name=_SRE_RUNBOOKS_COLLECTION,
                query_vector=embedding,
                limit=3,
            )

        from observability.metrics import SRE_RUNBOOK_HITS_TOTAL
        if results:
            SRE_RUNBOOK_HITS_TOTAL.inc()
            texts = [r.payload.get("text", "") for r in results if r.payload]
            return "\n\n---\n\n".join(texts)

    except Exception as exc:
        logger.warning(f"[diagnoser] search_runbooks falló: {exc}")
    return ""


def diagnose_with_llm(anomaly, vault_knowledge: str = "", metrics_knowledge: str = "") -> tuple[str, float]:
    """
    Genera diagnóstico de causa raíz usando el LLM con timeout 30s.
    Retorna (root_cause, confidence). Fallback determinístico si LLM falla.

    Migrado desde k8s-agent/main.py → diagnose_with_llm() (P2)
    """
    from observability.metrics import SRE_DIAGNOSIS_CONFIDENCE, SRE_DIAGNOSIS_LLM_TOTAL

    runbook = search_runbooks(anomaly.issue_type, anomaly.details)

    prompt = (
        f"Eres un SRE experto. Analiza esta anomalía en Kubernetes:\n\n"
        f"Tipo: {anomaly.issue_type}\n"
        f"Severidad: {anomaly.severity}\n"
        f"Namespace: {anomaly.namespace}\n"
        f"Recurso: {anomaly.resource_name} ({anomaly.resource_type})\n"
        f"Detalles: {anomaly.details}\n\n"
    )
    if runbook:
        prompt += f"Runbook relevante:\n{runbook[:1000]}\n\n"

    prompt += (
        "Responde con JSON exacto:\n"
        '{"root_cause": "...", "confidence": 0.0-1.0, "suggested_action": "..."}\n'
        "Sé conciso. root_cause máximo 2 oraciones."
    )

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_get_diag_llm().invoke, prompt)
            raw = future.result(timeout=30)
            # ChatOllama retorna AIMessage — extraer el contenido string
            if hasattr(raw, "content"):
                raw = raw.content

        import re
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            root_cause  = data.get("root_cause", anomaly.details)
            confidence  = float(data.get("confidence", 0.6))
            confidence  = max(0.0, min(1.0, confidence))

            SRE_DIAGNOSIS_LLM_TOTAL.labels(result="ok").inc()
            SRE_DIAGNOSIS_CONFIDENCE.observe(confidence)
            logger.info(
                f"[diagnoser] LLM diagnóstico: conf={confidence:.2f} "
                f"cause={root_cause[:80]!r}"
            )
            return root_cause, confidence

    except concurrent.futures.TimeoutError:
        logger.warning(f"[diagnoser] LLM timeout (30s) para {anomaly.issue_type}")
        SRE_DIAGNOSIS_LLM_TOTAL.labels(result="timeout").inc()
    except Exception as exc:
        logger.error(f"[diagnoser] LLM error: {exc}")
        SRE_DIAGNOSIS_LLM_TOTAL.labels(result="error").inc()

    # Fallback determinístico por tipo
    fallbacks = {
        "CRASH_LOOP":          ("Fallo en inicio del contenedor. Verificar logs y configuración.", 0.6),
        "OOM_KILLED":          ("Consumo de memoria superó el límite del contenedor.", 0.8),
        "IMAGE_PULL_ERROR":    ("No se puede descargar la imagen. Verificar registry y credenciales.", 0.9),
        "POD_PENDING_STUCK":   ("Recursos insuficientes o nodo sin capacidad de scheduling.", 0.7),
        "HIGH_CPU":            ("Carga de CPU elevada. Posible pico de tráfico o loop.", 0.6),
        "HIGH_MEMORY":         ("Consumo de memoria elevado. Posible memory leak.", 0.7),
        # Infraestructura
        "VAULT_SEALED":        ("Vault sellado tras reinicio del pod. Requiere unseal manual con claves Shamir.", 0.95),
        "LOADBALANCER_NO_IP":  ("MetalLB no pudo asignar IP. Verificar IPAddressPool y subred del nodo.", 0.85),
        "PVC_PENDING":         ("PVC sin volumen disponible. Verificar StorageClass y capacidad del nodo.", 0.80),
        "PVC_MOUNT_ERROR":     ("Fallo al montar volumen. Posible problema de permisos o volumen en uso.", 0.80),
        "DEPLOYMENT_DEGRADED": ("Deployment con réplicas insuficientes. Pod en fallo o nodo sin recursos.", 0.75),
        "SERVICE_NO_ENDPOINTS":("Service sin pods saludables. Verificar readiness probe y selector.", 0.75),
        "NODE_PRESSURE":       ("Nodo bajo presión de recursos. Disco, memoria o PIDs agotados.", 0.85),
        "K8S_EVENT_WARNING":   ("Evento de advertencia en infraestructura K8s. Ver detalles del evento.", 0.60),
    }
    cause, conf = fallbacks.get(anomaly.issue_type, (anomaly.details, 0.5))
    SRE_DIAGNOSIS_CONFIDENCE.observe(conf)
    return cause, conf


def adjust_confidence_with_history(
    anomaly,
    confidence: float,
    get_historical_fn,
) -> float:
    """
    Ajusta la confianza del diagnóstico combinando el score LLM con
    el historial de éxito de remediaciones previas (P3-B).

    Fórmula: confidence_final = 0.7 * llm_confidence + 0.3 * historical_rate

    Migrado desde k8s-agent/main.py → adjust_confidence_with_history()
    """
    from observability.metrics import SRE_LEARNING_ADJUSTED_TOTAL

    historical = get_historical_fn(
        issue_type=anomaly.issue_type,
        owner_name=anomaly.owner_name or anomaly.resource_name,
        namespace=anomaly.namespace,
    )
    if historical is None:
        return confidence

    adjusted = round(0.7 * confidence + 0.3 * historical, 3)
    SRE_LEARNING_ADJUSTED_TOTAL.inc()
    logger.info(
        f"[diagnoser] Confianza ajustada por historial: "
        f"{confidence:.2f} → {adjusted:.2f} (historical={historical:.2f})"
    )
    return adjusted


def maybe_save_runbook_entry(anomaly, root_cause: str, action_taken: str) -> None:
    """
    Auto-genera y guarda un runbook en Qdrant si la remediación fue exitosa (P4-D).

    Migrado desde k8s-agent/main.py → _maybe_save_runbook_entry()
    """
    from observability.metrics import SRE_AUTO_RUNBOOK_SAVED_TOTAL

    entry_text = (
        f"# Auto-runbook: {anomaly.issue_type}\n\n"
        f"**Recurso**: {anomaly.namespace}/{anomaly.resource_name}\n"
        f"**Detalles**: {anomaly.details}\n"
        f"**Causa raíz**: {root_cause}\n"
        f"**Acción tomada**: {action_taken}\n"
        f"**Timestamp**: {anomaly.timestamp.isoformat()}\n"
    )

    embedding = _get_embedding(entry_text)
    if not embedding:
        return

    try:
        import uuid

        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct

        client = QdrantClient(url=_QDRANT_URL)
        point = PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": entry_text,
                "issue_type": anomaly.issue_type,
                "namespace": anomaly.namespace,
                "resource_name": anomaly.resource_name,
                "auto_generated": True,
            },
        )
        client.upsert(collection_name=_SRE_RUNBOOKS_COLLECTION, points=[point])
        SRE_AUTO_RUNBOOK_SAVED_TOTAL.inc()
        logger.info(f"[diagnoser] Auto-runbook guardado para {anomaly.issue_type}")
    except Exception as exc:
        logger.warning(f"[diagnoser] No se pudo guardar runbook: {exc}")
