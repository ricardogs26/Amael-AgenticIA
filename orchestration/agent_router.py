"""
AgentRouter — mapea un request al agente o workflow apropiado.

Estrategia:
  1. Fast-path: keyword matching (determinístico, sin LLM, conf=0.9)
  2. Fallback LLM: clasificación semántica cuando no hay keyword match
  3. Default: workflow estándar (planner → executor → supervisor)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("orchestration.router")

_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5:14b")


@dataclass
class RoutingDecision:
    """Resultado del router: intent detectado y agentes a invocar."""
    intent: str
    agents: list[str]
    confidence: float
    routing_reason: str
    llm_classified: bool = False
    metadata: dict = field(default_factory=dict)


# ── Reglas keyword (orden importa: más específico primero) ────────────────────
_KEYWORD_RULES = [
    # SRE / incidentes
    (r"\b(incident|anomaly|crash\s*loop|oom|circuit.breaker|sre|postmortem|slo)\b",
     "sre", ["sre"]),
    # K8s / infraestructura
    (r"\b(pod|deployment|namespace|kubectl|k8s|kubernetes|cluster|node|ingress|service|pvc|helm)\b",
     "kubernetes", ["devops", "sre"]),
    # Monitoreo / métricas
    (r"\b(prometheus|grafana|metric|alert|latency|dashboard|log)\b",
     "monitoring", ["sre", "devops"]),
    # Productividad
    (r"\b(calendar|meeting|email|gmail|schedule|event|agenda|correo|cita|reunion)\b",
     "productivity", ["productivity"]),
    # CTO / estrategia tecnológica (antes que coding para evitar falsos positivos)
    (r"\b(estrategia|roadmap|visi[oó]n\s*t[eé]cn|tech\s*lead|cto|planificaci[oó]n\s*t[eé]cn|decisi[oó]n\s*t[eé]cn)\b",
     "cto", ["cto"]),
    # Arquitectura de software (antes que coding)
    (r"\b(adr|architecture\s*decision|dise[nñ]o\s*de\s*sistema|hexagonal|clean\s*arch|domain.driven|patr[oó]n\s*de\s*dise[nñ]o|contrato\s*api|api\s*contract)\b",
     "arch", ["arch"]),
    # Código / desarrollo
    (r"\b(code|function|class|refactor|implement|bug|error|exception|debug|script|pull\s*request|\bpr\b|commit)\b",
     "dev", ["dev"]),
    # Investigación / documentos
    (r"\b(search|find|look\s*up|documentation|explain|buscar|documento|pdf|docx)\b",
     "research", ["researcher"]),
    # Memoria / historial
    (r"\b(remember|recall|history|last\s*time|previously|recuerda|recordar)\b",
     "memory", ["memory"]),
    # QA / validación
    (r"\b(validate|test|check|verify|assert|validar|verificar|probar)\b",
     "qa", ["qa"]),
]


_INTENT_TO_AGENTS = {
    "sre":          ["sre"],
    "kubernetes":   ["devops", "sre"],
    "monitoring":   ["sre", "devops"],
    "productivity": ["productivity"],
    "cto":          ["cto"],
    "dev":          ["dev"],
    "arch":         ["arch"],
    "coding":       ["dev"],        # alias legacy → dev
    "research":     ["researcher"],
    "memory":       ["memory"],
    "qa":           ["qa"],
    "general":      ["planner", "executor", "supervisor"],
}

_LLM_ROUTING_PROMPT = """Clasifica la siguiente pregunta en uno de estos intents:
sre, kubernetes, monitoring, productivity, cto, dev, arch, research, memory, qa, general

Reglas:
- sre: incidentes, anomalías, circuit breaker, postmortem, SLO
- kubernetes: pods, deployments, namespaces, kubectl, cluster, nodos
- monitoring: prometheus, grafana, métricas, alertas, latencia, dashboards
- productivity: calendario, eventos, emails, gmail, agenda, organizar día
- cto: estrategia tecnológica, roadmap, visión, decisiones ejecutivas, planificación tech
- dev: código, función, clase, bug, implementar, refactorizar, script, pull request, commit
- arch: diseño de sistema, ADR, patrones de diseño, arquitectura hexagonal, contrato API
- research: buscar, documentos, PDF, explicar, investigar, búsqueda web
- memory: recordar, historial, conversación anterior
- qa: validar, probar, verificar, test
- general: cualquier otra cosa o preguntas generales

Pregunta: {question}

Responde ÚNICAMENTE con JSON:
{{"intent": "<intent>", "confidence": 0.0-1.0, "reason": "<una frase corta>"}}"""


def _record_routing_metric(intent: str, method: str) -> None:
    """Registra métricas de routing de forma best-effort."""
    try:
        from observability.metrics import ROUTER_DECISIONS_TOTAL
        ROUTER_DECISIONS_TOTAL.labels(intent=intent, method=method).inc()
    except Exception:
        pass


class AgentRouter:
    """
    Enruta un request al/los agente(s) apropiados.

    Estrategia:
      1. Keyword matching rápido (conf=0.9)
      2. Fallback LLM para queries ambiguas (conf variable)
      3. Default general (conf=0.5)

    Uso:
        router = AgentRouter()
        decision = await router.route("¿Cuál es el estado de los pods?")
        # → RoutingDecision(intent="kubernetes", agents=["devops","sre"], conf=0.9)
    """

    async def route(self, question: str) -> RoutingDecision:
        """
        Determina el intent y los agentes para manejar el request.
        """
        q_lower = question.lower()

        # 1. Keyword matching (determinístico, sin LLM)
        for pattern, intent, agents in _KEYWORD_RULES:
            if re.search(pattern, q_lower, re.IGNORECASE):
                _record_routing_metric(intent, "keyword")
                return RoutingDecision(
                    intent=intent,
                    agents=agents,
                    confidence=0.9,
                    routing_reason=f"keyword_match: {pattern[:40]}",
                    llm_classified=False,
                )

        # 2. Fallback LLM para queries ambiguas
        llm_decision = await self._route_with_llm(question)
        if llm_decision is not None:
            return llm_decision

        # 3. Default: workflow completo
        _record_routing_metric("general", "default")
        return RoutingDecision(
            intent="general",
            agents=["planner", "executor", "supervisor"],
            confidence=0.5,
            routing_reason="default_workflow",
            llm_classified=False,
        )

    async def _route_with_llm(self, question: str) -> RoutingDecision | None:
        """
        Clasifica el intent usando el LLM cuando el keyword matching no aplica.
        Timeout de 10s — si falla, retorna None para usar el default.
        """
        import concurrent.futures

        prompt = _LLM_ROUTING_PROMPT.format(question=question[:500])
        try:
            from skills.llm.skill import _get_ollama_llm
            llm = _get_ollama_llm()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(llm.invoke, prompt)
                raw    = future.result(timeout=10)

            raw = raw.strip() if isinstance(raw, str) else str(raw)
            # Extraer JSON de la respuesta
            match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if not match:
                logger.debug(f"[router] LLM no retornó JSON válido: {raw[:100]!r}")
                return None

            data       = json.loads(match.group())
            intent     = data.get("intent", "general").lower().strip()
            confidence = float(data.get("confidence", 0.7))
            reason     = data.get("reason", "llm_classification")

            # Asegurar que el intent es conocido
            if intent not in _INTENT_TO_AGENTS:
                intent = "general"

            agents = _INTENT_TO_AGENTS[intent]
            _record_routing_metric(intent, "llm")
            logger.info(
                f"[router] LLM routing: intent={intent!r} "
                f"conf={confidence:.2f} reason={reason!r}"
            )
            return RoutingDecision(
                intent=intent,
                agents=agents,
                confidence=confidence,
                routing_reason=f"llm: {reason}",
                llm_classified=True,
            )

        except concurrent.futures.TimeoutError:
            logger.debug("[router] LLM routing timeout (10s). Usando default.")
        except Exception as exc:
            logger.debug(f"[router] LLM routing error: {exc}. Usando default.")
        return None
