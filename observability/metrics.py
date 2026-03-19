"""
Métricas Prometheus centralizadas de toda la plataforma Amael-AgenticIA.

Todas las métricas se definen aquí para evitar duplicados y tener
un único lugar donde consultar qué se está midiendo.

Uso:
    from observability.metrics import PLANNER_LATENCY_SECONDS
    with PLANNER_LATENCY_SECONDS.time():
        ...
"""
from prometheus_client import Counter, Gauge, Histogram

# ── Planner ───────────────────────────────────────────────────────────────────
PLANNER_LATENCY_SECONDS = Histogram(
    "amael_planner_latency_seconds",
    "Latencia de la llamada LLM para generar el plan",
    buckets=(0.5, 1, 2, 4, 8, 15, 30),
)
PLANNER_PLAN_SIZE = Histogram(
    "amael_planner_plan_size_steps",
    "Número de pasos en el plan generado",
    buckets=(1, 2, 3, 4, 5, 6, 7, 8),
)
PLANNER_STEP_TYPES_TOTAL = Counter(
    "amael_planner_step_types_total",
    "Conteo de cada tipo de paso generado por el planner",
    ["step_type"],
)
PLANNER_PARSE_ERRORS_TOTAL = Counter(
    "amael_planner_parse_errors_total",
    "Fallos de parseo JSON en la salida del planner",
)
PLANNER_INVALID_STEPS_TOTAL = Counter(
    "amael_planner_invalid_steps_total",
    "Pasos con tipo desconocido descartados por _validate_plan",
)

# ── Executor ──────────────────────────────────────────────────────────────────
EXECUTOR_STEP_LATENCY_SECONDS = Histogram(
    "amael_executor_step_latency_seconds",
    "Latencia por paso de plan ejecutado",
    ["step_type"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
EXECUTOR_STEPS_TOTAL = Counter(
    "amael_executor_steps_total",
    "Total de pasos de plan ejecutados",
    ["step_type"],
)
EXECUTOR_ERRORS_TOTAL = Counter(
    "amael_executor_errors_total",
    "Fallos en la ejecución de pasos (tool ausente, excepción, etc.)",
    ["step_type"],
)
EXECUTOR_CONTEXT_TRUNCATIONS_TOTAL = Counter(
    "amael_executor_context_truncations_total",
    "Veces que el contexto acumulado fue truncado por límite de ventana LLM",
)
EXECUTOR_ESTIMATED_PROMPT_TOKENS = Histogram(
    "amael_executor_estimated_prompt_tokens",
    "Estimación de tokens enviados al LLM por paso REASONING (chars / 4)",
    ["step_type"],
    buckets=(100, 500, 1000, 2000, 4000, 8000, 16000, 32000),
)
EXECUTOR_PARALLEL_BATCH_SIZE = Histogram(
    "amael_executor_parallel_batch_size",
    "Número de pasos en un batch paralelo (>1 indica paralelismo real)",
    buckets=(1, 2, 3, 4, 5, 6),
)
EXECUTOR_PARALLEL_BATCHES_TOTAL = Counter(
    "amael_executor_parallel_batches_total",
    "Número de batches ejecutados con más de un paso en paralelo",
)

# ── Orchestrator ──────────────────────────────────────────────────────────────
ORCHESTRATOR_MAX_STEPS_HIT_TOTAL = Counter(
    "amael_orchestrator_max_steps_hit_total",
    "Veces que se alcanzó MAX_GRAPH_ITERATIONS antes de completar el plan",
)

# ── Supervisor ────────────────────────────────────────────────────────────────
SUPERVISOR_DECISIONS_TOTAL = Counter(
    "amael_supervisor_decisions_total",
    "Total de decisiones del supervisor por resultado",
    ["decision"],   # ACCEPT | REPLAN
)
SUPERVISOR_QUALITY_SCORE = Histogram(
    "amael_supervisor_quality_score",
    "Score de calidad (0-10) asignado por el supervisor a la respuesta final",
    buckets=(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
)
SUPERVISOR_REPLAN_TOTAL = Counter(
    "amael_supervisor_replan_total",
    "Veces que el supervisor disparó un re-plan",
)
SUPERVISOR_LATENCY_SECONDS = Histogram(
    "amael_supervisor_latency_seconds",
    "Latencia de la llamada LLM para evaluación del supervisor",
    buckets=(0.5, 1, 2, 4, 8, 15, 30),
)

# ── HTTP / API ────────────────────────────────────────────────────────────────
HTTP_REQUESTS_TOTAL = Counter(
    "amael_http_requests_total",
    "Total de requests HTTP recibidos",
    ["method", "handler", "status_code"],
)
HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    "amael_http_request_latency_seconds",
    "Latencia de requests HTTP",
    ["handler"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_TOKENS_TOTAL = Counter(
    "amael_llm_tokens_total",
    "Total de tokens procesados por el LLM",
    ["model", "token_type"],   # token_type: input | output
)
LLM_LATENCY_SECONDS = Histogram(
    "amael_llm_latency_seconds",
    "Latencia de llamadas al LLM",
    ["model"],
    buckets=(0.5, 1, 2, 4, 8, 15, 30, 60),
)
LLM_ERRORS_TOTAL = Counter(
    "amael_llm_errors_total",
    "Errores en llamadas al LLM",
    ["model", "error_type"],
)

# ── RAG ───────────────────────────────────────────────────────────────────────
RAG_HITS_TOTAL = Counter(
    "amael_rag_hits_total",
    "Búsquedas RAG que retornaron resultados relevantes",
)
RAG_MISS_TOTAL = Counter(
    "amael_rag_miss_total",
    "Búsquedas RAG que no retornaron resultados útiles",
)
RAG_LATENCY_SECONDS = Histogram(
    "amael_rag_latency_seconds",
    "Latencia de búsquedas en Qdrant",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2),
)

# ── Seguridad ─────────────────────────────────────────────────────────────────
SECURITY_RATE_LIMITED_TOTAL = Counter(
    "amael_security_rate_limited_total",
    "Requests bloqueados por rate limiting",
)
SECURITY_INPUT_BLOCKED_TOTAL = Counter(
    "amael_security_input_blocked_total",
    "Inputs bloqueados por validación de seguridad",
    ["reason"],   # too_long | injection_detected
)

# ── SRE Agent ─────────────────────────────────────────────────────────────────
SRE_LOOP_RUNS_TOTAL = Counter(
    "amael_sre_loop_runs_total",
    "Ejecuciones del loop autónomo SRE por resultado",
    ["result"],   # ok_clean | ok | error | maintenance | skipped_cb | skipped_not_leader
)
SRE_ANOMALIES_DETECTED_TOTAL = Counter(
    "amael_sre_anomalies_detected_total",
    "Anomalías detectadas por el SRE agent",
    ["severity", "issue_type"],
)
SRE_ACTIONS_TAKEN_TOTAL = Counter(
    "amael_sre_actions_taken_total",
    "Acciones ejecutadas por el SRE agent",
    ["action", "result"],
)
SRE_DIAGNOSIS_CONFIDENCE = Histogram(
    "amael_sre_diagnosis_confidence",
    "Confianza de diagnóstico LLM del SRE agent (0.0 a 1.0)",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
SRE_DIAGNOSIS_LLM_TOTAL = Counter(
    "amael_sre_diagnosis_llm_total",
    "Total de diagnósticos LLM del SRE agent por resultado",
    ["result"],   # ok | timeout | error
)
SRE_CIRCUIT_BREAKER_STATE = Gauge(
    "amael_sre_circuit_breaker_state",
    "Estado del circuit breaker: 0=cerrado, 1=abierto, 2=semi-abierto",
)
SRE_VERIFICATION_TOTAL = Counter(
    "amael_sre_verification_total",
    "Resultados de verificaciones post-acción",
    ["result"],   # healthy | unhealthy | error
)
SRE_ROLLBACK_TOTAL = Counter(
    "amael_sre_rollback_total",
    "Auto-rollbacks ejecutados por el SRE agent",
    ["result"],
)
SRE_SLO_VIOLATIONS_TOTAL = Counter(
    "amael_sre_slo_violations_total",
    "Violaciones de SLO detectadas",
    ["service"],
)
SRE_TREND_ANOMALIES_TOTAL = Counter(
    "amael_sre_trend_anomalies_total",
    "Anomalías predictivas detectadas por análisis de tendencias",
    ["issue_type"],
)
SRE_AUTO_RUNBOOK_SAVED_TOTAL = Counter(
    "amael_sre_auto_runbook_saved_total",
    "Runbooks auto-generados guardados en Qdrant",
)
SRE_WA_COMMANDS_TOTAL = Counter(
    "amael_sre_wa_commands_total",
    "Comandos WhatsApp /sre procesados",
    ["command"],
)
SRE_RUNBOOK_HITS_TOTAL = Counter(
    "amael_sre_runbook_hits_total",
    "Runbooks encontrados en Qdrant durante diagnóstico",
)
SRE_LEARNING_ADJUSTED_TOTAL = Counter(
    "amael_sre_learning_adjusted_total",
    "Diagnósticos cuya confianza fue ajustada por histórico de aprendizaje",
)
SRE_CORRELATION_GROUPED = Counter(
    "amael_sre_correlation_grouped_total",
    "Anomalías agrupadas por correlación multi-pod",
)
SRE_RESTART_LIMIT_HIT = Counter(
    "amael_sre_restart_limit_hit_total",
    "Veces que el guardrail de restart limit bloqueó una acción",
)
SRE_POSTMORTEM_TOTAL = Counter(
    "amael_sre_postmortem_total",
    "Postmortems LLM generados",
)
SRE_NOTIFY_TOTAL = Counter(
    "amael_sre_notify_total",
    "Notificaciones WhatsApp enviadas por el SRE agent",
    ["severity"],
)
SRE_MAINTENANCE_ACTIVE = Gauge(
    "amael_sre_maintenance_active",
    "1 si hay ventana de mantenimiento activa, 0 si no",
)
SRE_LANGGRAPH_REQUESTS = Counter(
    "amael_sre_langgraph_requests_total",
    "Requests al agente LangGraph del SRE (modo conversacional)",
    ["result"],   # ok | fallback | error
)

# ── Skills ────────────────────────────────────────────────────────────────────
SKILL_EXECUTIONS_TOTAL = Counter(
    "amael_skill_executions_total",
    "Total de ejecuciones de skills",
    ["skill_name", "success"],
)
SKILL_LATENCY_SECONDS = Histogram(
    "amael_skill_latency_seconds",
    "Latencia de ejecución de skills",
    ["skill_name"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
)

# ── Tools ─────────────────────────────────────────────────────────────────────
TOOL_EXECUTIONS_TOTAL = Counter(
    "amael_tool_executions_total",
    "Total de ejecuciones de tools de integración externa",
    ["tool_name", "success"],
)
TOOL_LATENCY_SECONDS = Histogram(
    "amael_tool_latency_seconds",
    "Latencia de ejecución de tools externas",
    ["tool_name"],
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
)

# ── Agentes (genérico) ────────────────────────────────────────────────────────
AGENT_EXECUTIONS_TOTAL = Counter(
    "amael_agent_executions_total",
    "Total de ejecuciones de agentes",
    ["agent_name", "success"],
)
AGENT_LATENCY_SECONDS = Histogram(
    "amael_agent_latency_seconds",
    "Latencia de ejecución de agentes",
    ["agent_name"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

# ── Dispatcher / Router ───────────────────────────────────────────────────────
AGENT_REQUESTS_TOTAL = Counter(
    "amael_agent_requests_total",
    "Total de requests procesados por el AgentDispatcher",
    ["intent", "mode", "result"],   # mode: direct | pipeline | error
)
DISPATCHER_LATENCY_SECONDS = Histogram(
    "amael_dispatcher_latency_seconds",
    "Latencia end-to-end del AgentDispatcher (routing + ejecución)",
    ["intent", "mode"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
ROUTER_DECISIONS_TOTAL = Counter(
    "amael_router_decisions_total",
    "Decisiones del AgentRouter por intent y método de clasificación",
    ["intent", "method"],   # method: keyword | llm | default
)
ROUTER_LLM_LATENCY_SECONDS = Histogram(
    "amael_router_llm_latency_seconds",
    "Latencia del LLM fallback en el AgentRouter",
    buckets=(0.1, 0.5, 1, 2, 4, 8, 15),
)

# ── Health / Registry ─────────────────────────────────────────────────────────
REGISTRY_HEALTH_STATUS = Gauge(
    "amael_registry_health_status",
    "Estado de salud de cada componente registrado (1=ok, 0=fail)",
    ["component_type", "component_name"],   # type: skill | tool | storage
)
