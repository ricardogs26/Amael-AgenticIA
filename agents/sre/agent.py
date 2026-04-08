"""
SRE Agent — agente autónomo de Site Reliability Engineering.

Responsabilidades:
  1. Ciclo autónomo Observe→Detect→Diagnose→Decide→Act→Report (APScheduler 60s)
  2. Agente conversacional via LangGraph ReAct (primary) + LangChain clásico (fallback)
  3. Inicialización de base de datos PostgreSQL (sre_incidents, sre_postmortems)
  4. Indexación de runbooks en Qdrant

Registro: @AgentRegistry.register → disponible como AgentRegistry.get("sre", ctx)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from agents.base.agent_registry import AgentRegistry
from core.agent_base import AgentResult, BaseAgent

logger = logging.getLogger("agents.sre.agent")

# ── Configuración ─────────────────────────────────────────────────────────────
_PROMETHEUS_URL     = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090",
)
_SRE_LOOP_ENABLED  = os.environ.get("SRE_LOOP_ENABLED", "true").lower() == "true"
_SRE_LOOP_INTERVAL = int(os.environ.get("SRE_LOOP_INTERVAL", "60"))
_RUNBOOKS_DIR      = os.path.join(os.path.dirname(__file__), "..", "..", "runbooks")
_QDRANT_URL        = os.environ.get("QDRANT_URL", "http://qdrant-service:6333")
_QDRANT_COLLECTION = "sre_runbooks"
_EMBED_MODEL       = "nomic-embed-text"
_OLLAMA_BASE_URL   = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")

# ── Singleton LLM ─────────────────────────────────────────────────────────────

_chat_llm = None
_classic_llm = None


def _get_chat_llm():
    """Chat LLM para LangGraph ReAct (bind_tools compatible)."""
    global _chat_llm
    if _chat_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _chat_llm = get_chat_llm()
    return _chat_llm


def _get_classic_llm():
    """Chat LLM para agente LangChain clásico (fallback)."""
    global _classic_llm
    if _classic_llm is None:
        from agents.base.llm_factory import get_chat_llm
        _classic_llm = get_chat_llm()
    return _classic_llm


# ── Knowledge Bases ───────────────────────────────────────────────────────────

def _load_kb(filename: str, label: str) -> str:
    """Carga un archivo de base de conocimiento y escapa llaves para LangChain."""
    # Buscar en el directorio del k8s-agent original
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "k8s-agent", filename),
        os.path.join(os.path.dirname(__file__), "..", "..", filename),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.exists(path):
            try:
                content = open(path).read().replace("{", "{{").replace("}", "}}")
                logger.info(f"[sre.agent] {label} cargado desde {path}")
                return content
            except Exception as exc:
                logger.warning(f"[sre.agent] Error leyendo {path}: {exc}")
    logger.warning(f"[sre.agent] {label} no encontrado ({filename})")
    return ""


_VAULT_KNOWLEDGE   = _load_kb("vault_knowledge.md",   "VAULT_KB")
_METRICS_KNOWLEDGE = _load_kb("metrics_knowledge.md", "METRICS_KB")

# ── SLO Targets ───────────────────────────────────────────────────────────────

_SLO_TARGETS: list[dict] = []


def load_slo_targets() -> None:
    """Carga los SLO targets desde la variable de entorno SLO_TARGETS_JSON."""
    global _SLO_TARGETS
    raw = os.environ.get("SLO_TARGETS_JSON", "[]")
    try:
        _SLO_TARGETS = json.loads(raw)
        logger.info(f"[sre.agent] {len(_SLO_TARGETS)} SLO targets cargados.")
    except Exception as exc:
        logger.error(f"[sre.agent] Error parseando SLO_TARGETS_JSON: {exc}")
        _SLO_TARGETS = []


# ── PostgreSQL — init tablas ──────────────────────────────────────────────────

def init_sre_db() -> None:
    """
    Crea las tablas sre_incidents y sre_postmortems si no existen.
    Usa el pool de storage.postgres para compatibilidad con el resto de la plataforma.
    """
    try:
        from storage.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Tabla de incidentes (P1)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sre_incidents (
                        id            SERIAL PRIMARY KEY,
                        incident_key  TEXT UNIQUE,
                        created_at    TIMESTAMPTZ DEFAULT now(),
                        namespace     TEXT,
                        resource_name TEXT,
                        resource_type TEXT,
                        issue_type    TEXT,
                        severity      TEXT,
                        details       TEXT,
                        root_cause    TEXT,
                        confidence    FLOAT,
                        action_taken  TEXT,
                        action_result TEXT,
                        notified      BOOLEAN DEFAULT false
                    );
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sre_ts "
                    "ON sre_incidents(created_at DESC);"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sre_issue "
                    "ON sre_incidents(issue_type, namespace);"
                )
                # Tabla de postmortems (P5-D)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sre_postmortems (
                        id                  SERIAL PRIMARY KEY,
                        incident_key        TEXT UNIQUE,
                        created_at          TIMESTAMPTZ DEFAULT now(),
                        namespace           TEXT,
                        resource_name       TEXT,
                        issue_type          TEXT,
                        impact              TEXT,
                        timeline            TEXT,
                        root_cause_summary  TEXT,
                        resolution          TEXT,
                        prevention          TEXT,
                        action_items        TEXT,
                        raw_json            TEXT
                    );
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pm_ts "
                    "ON sre_postmortems(created_at DESC);"
                )
        logger.info("[sre.agent] Tablas sre_incidents y sre_postmortems listas.")
    except Exception as exc:
        logger.error(f"[sre.agent] init_sre_db error: {exc}")


# ── Qdrant — init runbooks ────────────────────────────────────────────────────

def init_runbooks_qdrant() -> None:
    """
    Crea la colección sre_runbooks en Qdrant e indexa los archivos de runbook
    del directorio runbooks/ si la colección está vacía.
    """
    try:
        import uuid

        import requests as _req
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams

        client = QdrantClient(url=_QDRANT_URL)

        # Crear colección si no existe
        existing = {c.name for c in client.get_collections().collections}
        if _QDRANT_COLLECTION not in existing:
            # Obtener dimensión del modelo
            probe = _req.post(
                f"{_OLLAMA_BASE_URL}/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": "probe"},
                timeout=15,
            )
            dim = len(probe.json().get("embedding", [768] * 768))
            if dim == 0:
                dim = 768
            client.create_collection(
                collection_name=_QDRANT_COLLECTION,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            logger.info(f"[sre.agent] Colección '{_QDRANT_COLLECTION}' creada (dim={dim}).")

        # Verificar si ya tiene documentos
        count = client.count(collection_name=_QDRANT_COLLECTION).count
        if count > 0:
            logger.info(f"[sre.agent] Runbooks ya indexados ({count} docs). Skipping.")
            return

        # Indexar archivos markdown del directorio runbooks/
        runbooks_dir = os.path.normpath(_RUNBOOKS_DIR)
        if not os.path.isdir(runbooks_dir):
            logger.warning(f"[sre.agent] Directorio runbooks no encontrado: {runbooks_dir}")
            return

        points = []
        import glob as _glob
        for path in _glob.glob(os.path.join(runbooks_dir, "*.md")):
            try:
                text = open(path).read()
                resp = _req.post(
                    f"{_OLLAMA_BASE_URL}/api/embeddings",
                    json={"model": _EMBED_MODEL, "prompt": text[:2000]},
                    timeout=30,
                )
                embedding = resp.json().get("embedding")
                if not embedding:
                    continue
                points.append(PointStruct(
                    id=str(uuid.uuid4()),
                    vector=embedding,
                    payload={
                        "text": text,
                        "source": os.path.basename(path),
                        "auto_generated": False,
                    },
                ))
            except Exception as exc:
                logger.warning(f"[sre.agent] Error indexando {path}: {exc}")

        if points:
            client.upsert(collection_name=_QDRANT_COLLECTION, points=points)
            logger.info(f"[sre.agent] {len(points)} runbooks indexados en Qdrant.")

    except Exception as exc:
        logger.warning(f"[sre.agent] init_runbooks_qdrant error: {exc}")


# ── Vault question detection ──────────────────────────────────────────────────

_VAULT_KEYWORDS = {
    "vault", "secret", "secrets", "token", "credencial", "credenciales",
    "policy", "policies", "auth", "hvs", "kv", "pki", "unseal",
}


def _is_vault_question(query: str) -> bool:
    """Detecta preguntas sobre Vault para responder directamente sin RAG."""
    q = query.lower()
    return any(kw in q for kw in _VAULT_KEYWORDS)


# ── LangChain Tools (para ReAct) ──────────────────────────────────────────────

def _build_tools() -> list:
    """Construye la lista de LangChain Tools para el agente SRE."""
    from langchain_core.tools import StructuredTool

    from agents.sre import healer, reporter, scheduler

    # Importaciones lazy para los tools del agente conversacional
    def _get_pods(ns: str = "") -> str:
        try:
            from kubernetes import client as k8s
            from kubernetes import config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            v1 = k8s.CoreV1Api()
            namespace = ns.strip() or os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
            pods = v1.list_namespaced_pod(namespace=namespace)
            lines = []
            for p in pods.items:
                phase = p.status.phase or "Unknown"
                restarts = sum(
                    cs.restart_count
                    for cs in (p.status.container_statuses or [])
                )
                lines.append(f"{p.metadata.name}: {phase}, restarts={restarts}")
            return "\n".join(lines) or "No pods encontrados."
        except Exception as exc:
            return f"Error listando pods: {exc}"

    def _describe_pod(pod_name: str) -> str:
        try:
            from kubernetes import client as k8s
            from kubernetes import config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            v1 = k8s.CoreV1Api()
            namespace = os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
            pod = v1.read_namespaced_pod(name=pod_name.strip(), namespace=namespace)
            events = v1.list_namespaced_event(namespace=namespace,
                                               field_selector=f"involvedObject.name={pod_name.strip()}")
            cs_info = []
            for cs in (pod.status.container_statuses or []):
                state = cs.state
                if state.waiting:
                    cs_info.append(f"{cs.name}: WAITING ({state.waiting.reason})")
                elif state.terminated:
                    cs_info.append(f"{cs.name}: TERMINATED ({state.terminated.reason})")
                else:
                    cs_info.append(f"{cs.name}: RUNNING, restarts={cs.restart_count}")
            ev_list = [
                f"  [{e.type}] {e.reason}: {e.message}"
                for e in events.items[-5:]
            ]
            return (
                f"Pod: {pod_name}\n"
                f"Phase: {pod.status.phase}\n"
                f"Containers: {'; '.join(cs_info)}\n"
                f"Events:\n" + "\n".join(ev_list)
            )
        except Exception as exc:
            return f"Error describiendo pod {pod_name}: {exc}"

    def _restart_deployment(name: str) -> str:
        parts = [p.strip() for p in name.split(",")]
        deployment = parts[0]
        namespace = parts[1] if len(parts) > 1 else os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
        return healer.rollout_restart(deployment, namespace)

    def _rollback_deployment(name: str) -> str:
        parts = [p.strip() for p in name.split(",")]
        deployment = parts[0]
        namespace = parts[1] if len(parts) > 1 else os.environ.get("DEFAULT_NAMESPACE", "amael-ia")
        return healer.rollout_undo_deployment(deployment, namespace)

    def _get_prometheus_metrics(query: str) -> str:
        try:
            import requests as _req
            resp = _req.get(
                f"{_PROMETHEUS_URL}/api/v1/query",
                params={"query": query.strip()},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("status") == "success":
                results = resp.json()["data"]["result"]
                if not results:
                    return "Sin datos para esa query."
                lines = []
                for r in results[:10]:
                    metric = r.get("metric", {})
                    value  = r.get("value", [None, "N/A"])[1]
                    lines.append(f"{metric}: {value}")
                return "\n".join(lines)
            return f"Prometheus error: {resp.status_code}"
        except Exception as exc:
            return f"Error consultando Prometheus: {exc}"

    def _search_runbooks(query: str) -> str:
        from agents.sre.diagnoser import search_runbooks
        parts = query.split(":", 1)
        issue_type = parts[0].strip()
        details    = parts[1].strip() if len(parts) > 1 else query
        result = search_runbooks(issue_type, details)
        return result or "No se encontraron runbooks relevantes."

    def _notify_whatsapp(message: str) -> str:
        success = reporter.notify_whatsapp_sre(message, severity="HIGH")
        return "✅ Notificación enviada." if success else "❌ Error enviando notificación."

    def _activate_maint(minutes: str) -> str:
        try:
            m = int(minutes.strip()) if minutes.strip().isdigit() else 60
        except Exception:
            m = 60
        return scheduler.activate_maintenance(m)

    def _deactivate_maint() -> str:
        return scheduler.deactivate_maintenance()

    def _get_sre_status() -> str:
        state = scheduler.get_loop_state()
        return (
            f"Loop: {'enabled' if state.loop_enabled else 'disabled'}\n"
            f"Circuit Breaker: {state.circuit_breaker_state}\n"
            f"Maintenance: {state.maintenance_active}\n"
            f"Last run: {state.last_run_at}\n"
            f"Last result: {state.last_run_result}\n"
            f"Anomalies: {state.anomalies_in_last_run}, Actions: {state.actions_in_last_run}"
        )

    def _vault_query(query: str) -> str:
        """Responde preguntas de Vault usando la knowledge base."""
        if _VAULT_KNOWLEDGE:
            return (
                "Base de conocimiento Vault:\n\n"
                + _VAULT_KNOWLEDGE.replace("{{", "{").replace("}}", "}")[:3000]
                + "\n\nPregunta: " + query
            )
        return "No se encontró la base de conocimiento de Vault."

    return [
        StructuredTool.from_function(func=_get_pods,              name="Listar_Pods",
            description="Lista pods de un namespace con su estado y reinicios. Parámetro: ns (namespace, opcional)."),
        StructuredTool.from_function(func=_describe_pod,          name="Describir_Pod",
            description="Describe un pod y sus eventos. Parámetro: pod_name."),
        StructuredTool.from_function(func=_restart_deployment,    name="Reiniciar_Deployment",
            description="Ejecuta rollout restart de un deployment. Parámetro: name ('deployment' o 'deployment, namespace')."),
        StructuredTool.from_function(func=_rollback_deployment,   name="Revertir_Deployment",
            description="Rollback de deployment a revisión anterior. Parámetro: name ('deployment' o 'deployment, namespace')."),
        StructuredTool.from_function(func=_get_prometheus_metrics, name="Consultar_Prometheus",
            description="Ejecuta una query PromQL. Parámetro: query."),
        StructuredTool.from_function(func=_search_runbooks,       name="Consultar_Base_Conocimiento",
            description="Busca runbooks y procedimientos. Parámetro: query ('tipo_error: descripción')."),
        StructuredTool.from_function(func=_vault_query,           name="Consultar_Vault",
            description="Consulta información sobre Vault, secrets, policies. Usar SIEMPRE para preguntas de Vault. Parámetro: query."),
        StructuredTool.from_function(func=_get_sre_status,        name="Estado_SRE",
            description="Retorna el estado actual del loop SRE autónomo, circuit breaker y mantenimiento. Sin parámetros."),
        StructuredTool.from_function(func=_notify_whatsapp,       name="Notificar_WhatsApp",
            description="Envía alerta SRE urgente por WhatsApp. Parámetro: message."),
        StructuredTool.from_function(func=_activate_maint,        name="Activar_Mantenimiento",
            description="Pausa el loop SRE durante mantenimiento. Parámetro: minutes (duración en minutos)."),
        StructuredTool.from_function(func=_deactivate_maint,      name="Desactivar_Mantenimiento",
            description="Reactiva el loop SRE después de mantenimiento. Sin parámetros."),
    ]


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    vault_section   = _VAULT_KNOWLEDGE   or "(no disponible)"
    metrics_section = _METRICS_KNOWLEDGE or "(no disponible)"
    return (
        "Eres un SRE Senior experto en Kubernetes y HashiCorp Vault.\n"
        "Tu objetivo es diagnosticar y resolver problemas del clúster.\n\n"
        "=== HASHICORP VAULT ===\n"
        + vault_section +
        "\n=== FIN VAULT ===\n\n"
        "=== MÉTRICAS PROMETHEUS ===\n"
        + metrics_section +
        "\n=== FIN MÉTRICAS ===\n\n"
        "REGLAS:\n"
        "1. Pods con problemas → Describir_Pod primero (contiene Events con causa raíz).\n"
        "2. Vault → Consultar_Vault siempre.\n"
        "3. Runbooks y soluciones → Consultar_Base_Conocimiento.\n"
        "4. Reiniciar → Reiniciar_Deployment (no eliminar pod salvo necesidad crítica).\n"
        "5. Problema crítico → Notificar_WhatsApp.\n"
        "6. Memoria de pods: reportar siempre en MB.\n"
        "7. Mencionar siempre el tipo de recurso (CPU o Memoria).\n"
        "8. Responder DIRECTAMENTE con datos técnicos, sin introducciones ni cortesías.\n\n"
        "FORMATO:\n"
        "Thought: | Action: | Action Input: | Observation:\n"
        "Final Answer: [respuesta detallada]\n\n"
        "Herramientas disponibles:"
    )


# ── LangGraph ReAct agent ─────────────────────────────────────────────────────

_langgraph_agent   = None
_langgraph_enabled = False


def _get_langgraph_agent():
    """Lazy init del agente LangGraph ReAct."""
    global _langgraph_agent, _langgraph_enabled
    if _langgraph_agent is not None:
        return _langgraph_agent

    try:
        from langchain_core.messages import SystemMessage
        from langgraph.prebuilt import create_react_agent
        _langgraph_agent = create_react_agent(
            _get_chat_llm(),
            _build_tools(),
            prompt=SystemMessage(content=_build_system_prompt()),
        )
        _langgraph_enabled = True
        logger.info("[sre.agent] LangGraph ReAct compilado correctamente.")
    except Exception as exc:
        logger.error(f"[sre.agent] Error compilando LangGraph: {exc}")
    return _langgraph_agent


def query_agent(query: str) -> str:
    """
    Punto de entrada para consultas conversacionales al agente SRE.

    Usa LangGraph ReAct (primary) con fallback a LangChain clásico.
    Las preguntas sobre Vault utilizan la knowledge base directamente.
    """
    from observability.metrics import SRE_LANGGRAPH_REQUESTS

    # Fast-path: Vault questions
    if _is_vault_question(query) and _VAULT_KNOWLEDGE:
        logger.info("[sre.agent] Vault question: bypassing RAG, using KB directly.")
        vault_context = _VAULT_KNOWLEDGE.replace("{{", "{").replace("}}", "}")
        prompt = (
            f"Contexto Vault:\n{vault_context[:3000]}\n\n"
            f"Pregunta: {query}\n\n"
            "Responde con información técnica precisa basada en el contexto."
        )
        try:
            raw = _get_classic_llm().invoke(prompt)
            return raw.strip() if isinstance(raw, str) else str(raw)
        except Exception as exc:
            logger.error(f"[sre.agent] Vault LLM error: {exc}")
            return f"❌ Error consultando Vault: {exc}"

    # LangGraph ReAct
    lg_agent = _get_langgraph_agent()
    if lg_agent is None:
        return "❌ Agente SRE no disponible: error inicializando LangGraph."
    try:
        from langchain_core.messages import HumanMessage
        result = lg_agent.invoke({"messages": [HumanMessage(content=query)]})
        messages = result.get("messages", [])
        if messages:
            last = messages[-1]
            content = last.content if hasattr(last, "content") else str(last)
            SRE_LANGGRAPH_REQUESTS.labels(result="ok").inc()
            return content
        SRE_LANGGRAPH_REQUESTS.labels(result="ok").inc()
        return "Sin respuesta del agente."
    except Exception as exc:
        logger.error(f"[sre.agent] LangGraph error: {exc}")
        SRE_LANGGRAPH_REQUESTS.labels(result="error").inc()
        return f"❌ Error ejecutando agente SRE: {exc}"


# ── APScheduler — loop autónomo ───────────────────────────────────────────────

_sre_scheduler = None


def start_sre_loop() -> Any | None:
    """
    Inicia el APScheduler con el loop autónomo SRE cada SRE_LOOP_INTERVAL segundos.

    Retorna la instancia del scheduler para que el servidor FastAPI
    pueda registrarla en app.state y apagarla en shutdown.
    """
    global _sre_scheduler

    if not _SRE_LOOP_ENABLED:
        logger.info("[sre.agent] SRE loop deshabilitado (SRE_LOOP_ENABLED=false).")
        return None

    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        from agents.sre.scheduler import sre_autonomous_loop

        _sre_scheduler = BackgroundScheduler(timezone="UTC")
        _sre_scheduler.add_job(
            sre_autonomous_loop,
            "interval",
            seconds=_SRE_LOOP_INTERVAL,
            kwargs={
                "prometheus_url":    _PROMETHEUS_URL,
                "slo_targets":       _SLO_TARGETS,
                "vault_knowledge":   _VAULT_KNOWLEDGE,
                "metrics_knowledge": _METRICS_KNOWLEDGE,
            },
            id="sre_autonomous_loop",
            replace_existing=True,
        )
        _sre_scheduler.start()
        # Registrar scheduler en healer para verificación post-acción (P3-A)
        from agents.sre import healer as _healer
        _healer.set_aps_scheduler(_sre_scheduler)
        logger.info(
            f"[sre.agent] Loop autónomo iniciado (interval={_SRE_LOOP_INTERVAL}s)."
        )
        return _sre_scheduler

    except ImportError as exc:
        logger.warning(f"[sre.agent] APScheduler no disponible ({exc}). Loop no iniciado.")
    except Exception as exc:
        logger.error(f"[sre.agent] Error iniciando SRE loop: {exc}")
    return None


def stop_sre_loop() -> None:
    """Apaga el scheduler del loop SRE."""
    global _sre_scheduler
    if _sre_scheduler is not None:
        try:
            _sre_scheduler.shutdown(wait=False)
            logger.info("[sre.agent] Scheduler SRE detenido.")
        except Exception as exc:
            logger.warning(f"[sre.agent] Error deteniendo scheduler: {exc}")
        _sre_scheduler = None


def get_scheduler() -> Any | None:
    """Retorna la instancia del scheduler (para schedule_verification en healer)."""
    return _sre_scheduler


# ── SREAgent — BaseAgent implementation ──────────────────────────────────────

@AgentRegistry.register
class RaphaelAgent(BaseAgent):
    """
    Raphael — SRE Agent conversacional registrado en el AgentRegistry.

    Punto de entrada para consultas sobre el estado del clúster,
    incidentes, Vault y acciones de remediación via lenguaje natural.

    El ciclo autónomo (APScheduler) se inicia a través de start_sre_loop()
    en el lifespan del servidor FastAPI — no en el __init__ del agente.
    """

    name         = "raphael"
    role         = "Site Reliability Engineering — Kubernetes Autonomous Agent"
    version      = "5.0.2"
    capabilities = [
        "observe_cluster",
        "detect_anomalies",
        "diagnose_with_llm",
        "rollout_restart",
        "auto_rollback",
        "maintenance_window",
        "slo_monitoring",
        "postmortem_generation",
        "whatsapp_alerts",
        "leader_election",
    ]

    async def execute(self, task: dict[str, Any]) -> AgentResult:
        """
        Ejecuta una consulta conversacional al agente SRE.

        Args:
            task: {"query": str, ...}

        Returns:
            AgentResult con la respuesta del agente.
        """
        query = task.get("query", "").strip()
        if not query:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error="query vacía",
            )

        try:
            response = query_agent(query)
            return AgentResult(
                success=True,
                output={"response": response},
                agent_name=self.name,
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                output=None,
                agent_name=self.name,
                error=str(exc),
            )
