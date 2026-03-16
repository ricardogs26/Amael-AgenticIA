"""
AgentDispatcher — ejecuta el routing decision invocando el agente correcto.

Dos caminos de ejecución:
  - Directo: SRE, Productivity, Researcher → BaseAgent.run() sin LangGraph
  - Pipeline: general, kubernetes, monitoring, coding, qa → LangGraph workflow

El dispatcher construye el AgentContext via ContextFactory, instancia el agente
via AgentRegistry, y retorna un resultado unificado.

Uso:
    dispatcher = AgentDispatcher()
    result = await dispatcher.dispatch(
        question="¿Qué pods están en CrashLoop?",
        user_id="user@example.com",
        tools_map=tools,
        routing_decision=decision,
    )
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from orchestration.agent_router import RoutingDecision

logger = logging.getLogger("orchestration.dispatcher")

# Intents que van directamente a un agente específico (sin LangGraph)
_DIRECT_DISPATCH: Dict[str, str] = {
    "sre":          "sre",
    "productivity": "productivity",
    "research":     "researcher",
    "cto":          "cto",
    "dev":          "dev",
    "arch":         "arch",
    "coding":       "dev",     # alias legacy
}

# Intents que pasan por el pipeline LangGraph completo
_PIPELINE_INTENTS = {"general", "kubernetes", "monitoring", "qa", "memory"}


class AgentDispatcher:
    """
    Despacha un request al camino de ejecución correcto.

    Prioridad de decisión:
      1. intent en _DIRECT_DISPATCH → agente directo (más rápido)
      2. intent en _PIPELINE_INTENTS → LangGraph workflow
      3. fallback → LangGraph workflow (comportamiento seguro)
    """

    async def dispatch(
        self,
        question: str,
        user_id: str,
        tools_map: Dict[str, Any],
        routing_decision: Optional[RoutingDecision] = None,
        request_id: str = "",
        conversation_id: str = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Ejecuta el request según el routing_decision.

        Args:
            question:         Pregunta del usuario.
            user_id:          Email / ID del usuario.
            tools_map:        Herramientas para el executor del LangGraph.
            routing_decision: Decisión del AgentRouter. Si None, usa pipeline.
            request_id:       UUID de correlación. Auto-generado si vacío.
            conversation_id:  ID de la conversación activa.
            extra_metadata:   Metadatos adicionales para el contexto.

        Returns:
            Dict con al menos {"final_answer": str, "intent": str, "dispatch_mode": str}
        """
        if not request_id:
            request_id = str(uuid.uuid4())

        intent = routing_decision.intent if routing_decision else "general"
        start  = time.time()

        try:
            if intent in _DIRECT_DISPATCH:
                result = await self._dispatch_direct(
                    agent_name=_DIRECT_DISPATCH[intent],
                    question=question,
                    user_id=user_id,
                    intent=intent,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    extra_metadata=extra_metadata,
                )
            else:
                result = await self._dispatch_pipeline(
                    question=question,
                    user_id=user_id,
                    tools_map=tools_map,
                    request_id=request_id,
                    conversation_id=conversation_id,
                )

            elapsed_ms = (time.time() - start) * 1000
            result["intent"]        = intent
            result["request_id"]    = request_id
            result["elapsed_ms"]    = round(elapsed_ms, 1)
            result["llm_classified"] = (
                routing_decision.llm_classified if routing_decision else False
            )
            self._record_metrics(
                intent,
                result.get("dispatch_mode", "pipeline"),
                success=True,
                elapsed_ms=round((time.time() - start) * 1000, 1),
            )
            return result

        except Exception as exc:
            logger.error(f"[dispatcher] Error en dispatch (intent={intent}): {exc}", exc_info=True)
            self._record_metrics(intent, "error", success=False)
            return {
                "final_answer":   f"❌ Error procesando tu request: {exc}",
                "intent":         intent,
                "request_id":     request_id,
                "dispatch_mode":  "error",
                "error":          str(exc),
            }

    async def _dispatch_direct(
        self,
        agent_name: str,
        question: str,
        user_id: str,
        intent: str,
        request_id: str,
        conversation_id: str,
        extra_metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Invoca un agente especializado directamente, sin pasar por LangGraph.
        Más rápido para intents claros (SRE, Productivity, Researcher).
        """
        from agents.base.agent_registry import AgentRegistry
        from orchestration.context_factory import ContextFactory

        # Construir contexto especializado por agente
        if intent == "sre":
            ctx = ContextFactory.build_sre_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"query": question}

        elif intent == "productivity":
            ctx = ContextFactory.build_productivity_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"user_email": user_id, "query": question}

        elif intent == "research":
            ctx = ContextFactory.build_researcher_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"query": question, "user_email": user_id}

        elif intent in ("cto", "coding"):
            ctx = ContextFactory.build_cto_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"query": question, "user_email": user_id}

        elif intent == "dev":
            ctx = ContextFactory.build_dev_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"query": question, "user_email": user_id}

        elif intent == "arch":
            ctx = ContextFactory.build_arch_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
            )
            task = {"query": question, "user_email": user_id}

        else:
            ctx = ContextFactory.build_context(
                user_id=user_id,
                request_id=request_id,
                conversation_id=conversation_id,
                metadata=extra_metadata,
            )
            task = {"query": question}

        try:
            agent  = AgentRegistry.get(agent_name, ctx)
            result = await agent.run(task)
        except Exception as exc:
            logger.error(f"[dispatcher] Agente '{agent_name}' error: {exc}")
            raise

        # Normalizar output del agente directo al formato unificado
        answer = ""
        if result.success and result.output:
            output = result.output
            if isinstance(output, dict):
                answer = (
                    output.get("response")
                    or output.get("summary")
                    or output.get("result")
                    or str(output)
                )
            else:
                answer = str(output)
        elif not result.success:
            answer = f"❌ {result.error or 'Error desconocido'}"

        return {
            "final_answer":  answer,
            "dispatch_mode": "direct",
            "agent":         agent_name,
            "duration_ms":   result.duration_ms,
            "success":       result.success,
        }

    async def _dispatch_pipeline(
        self,
        question: str,
        user_id: str,
        tools_map: Dict[str, Any],
        request_id: str,
        conversation_id: str,
    ) -> Dict[str, Any]:
        """
        Ejecuta el pipeline LangGraph completo:
        planner → grouper → batch_executor → supervisor
        """
        from orchestration.workflow_engine import run_workflow

        state = await run_workflow(
            question=question,
            user_id=user_id,
            tools_map=tools_map,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        return {
            "final_answer":       state.get("final_answer") or "",
            "dispatch_mode":      "pipeline",
            "supervisor_score":   state.get("supervisor_score", 0),
            "supervisor_decision": state.get("supervisor_decision", ""),
            "retry_count":        state.get("retry_count", 0),
            "plan":               state.get("plan", []),
        }

    def _record_metrics(
        self,
        intent: str,
        mode: str,
        success: bool,
        elapsed_ms: float = 0.0,
    ) -> None:
        """Registra métricas Prometheus del dispatcher."""
        try:
            from observability.metrics import AGENT_REQUESTS_TOTAL, DISPATCHER_LATENCY_SECONDS
            result = "ok" if success else "error"
            AGENT_REQUESTS_TOTAL.labels(intent=intent, mode=mode, result=result).inc()
            if elapsed_ms > 0:
                DISPATCHER_LATENCY_SECONDS.labels(intent=intent, mode=mode).observe(
                    elapsed_ms / 1000
                )
        except Exception:
            pass  # Métricas son best-effort


# ── Función de conveniencia ───────────────────────────────────────────────────

_dispatcher = AgentDispatcher()


async def dispatch(
    question: str,
    user_id: str,
    tools_map: Dict[str, Any],
    routing_decision: Optional[RoutingDecision] = None,
    request_id: str = "",
    conversation_id: str = "",
) -> Dict[str, Any]:
    """
    Shortcut funcional que usa la instancia global del dispatcher.

    Equivalente a:
        await AgentDispatcher().dispatch(...)
    """
    return await _dispatcher.dispatch(
        question=question,
        user_id=user_id,
        tools_map=tools_map,
        routing_decision=routing_decision,
        request_id=request_id,
        conversation_id=conversation_id,
    )
