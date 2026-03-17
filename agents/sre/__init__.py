"""
agents.sre — Autonomous SRE Agent package.

Módulos:
  models    — Anomaly, SREAction, SRELoopState dataclasses
  observer  — observe_cluster, observe_metrics, observe_trends, observe_slo
  detector  — detect_anomalies, correlate_anomalies
  diagnoser — diagnose_with_llm, search_runbooks, adjust_confidence_with_history
  healer    — decide_action, execute_sre_action, rollout_restart, rollout_undo_deployment
  reporter  — store_incident, notify_whatsapp_sre, get_recent_incidents
  scheduler — sre_autonomous_loop, CircuitBreaker, get_loop_state
  agent     — SREAgent (BaseAgent), start_sre_loop, init_sre_db, init_runbooks_qdrant
"""
from agents.sre.agent import (
    SREAgent,
    get_scheduler,
    init_runbooks_qdrant,
    init_sre_db,
    load_slo_targets,
    query_agent,
    start_sre_loop,
    stop_sre_loop,
)
from agents.sre.models import Anomaly, SREAction, SRELoopState
from agents.sre.reporter import (
    get_historical_success_rate,
    get_recent_incidents,
    get_recent_postmortems,
)
from agents.sre.scheduler import activate_maintenance, deactivate_maintenance, get_loop_state

__all__ = [
    # Agent entry points
    "SREAgent",
    "init_sre_db",
    "init_runbooks_qdrant",
    "load_slo_targets",
    "start_sre_loop",
    "stop_sre_loop",
    "get_scheduler",
    "query_agent",
    # Loop control
    "get_loop_state",
    "activate_maintenance",
    "deactivate_maintenance",
    # Data access
    "get_recent_incidents",
    "get_recent_postmortems",
    "get_historical_success_rate",
    # Models
    "Anomaly",
    "SREAction",
    "SRELoopState",
]
