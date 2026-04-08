"""
Enumeraciones y constantes globales de la plataforma Amael-AgenticIA.
"""
from enum import StrEnum


class StepType(StrEnum):
    """Tipos de pasos que puede generar el PlannerAgent."""
    K8S_TOOL          = "K8S_TOOL"
    RAG_RETRIEVAL     = "RAG_RETRIEVAL"
    PRODUCTIVITY_TOOL = "PRODUCTIVITY_TOOL"
    WEB_SEARCH        = "WEB_SEARCH"
    DOCUMENT_TOOL     = "DOCUMENT_TOOL"
    TTS_TOOL          = "TTS_TOOL"
    CODE_GENERATION   = "CODE_GENERATION"   # Gabriel: leer/escribir código en GitHub
    REASONING         = "REASONING"


class ActionType(StrEnum):
    """Acciones de remediación que puede ejecutar el SREAgent."""
    ROLLOUT_RESTART       = "ROLLOUT_RESTART"
    ROLLOUT_UNDO          = "ROLLOUT_UNDO_DEPLOYMENT"
    NOTIFY_HUMAN          = "NOTIFY_HUMAN"
    SCALE_DEPLOYMENT      = "SCALE_DEPLOYMENT"
    NO_ACTION             = "NO_ACTION"


class AnomalyType(StrEnum):
    """Tipos de anomalías detectables por el SREAgent."""
    # Estructurales (K8s observe)
    CRASH_LOOP              = "CRASH_LOOP"
    OOM_KILLED              = "OOM_KILLED"
    IMAGE_PULL_ERROR        = "IMAGE_PULL_ERROR"
    POD_FAILED              = "POD_FAILED"
    POD_PENDING_STUCK       = "POD_PENDING_STUCK"
    HIGH_RESTARTS           = "HIGH_RESTARTS"
    NODE_NOT_READY          = "NODE_NOT_READY"
    # Métricas (Prometheus)
    HIGH_CPU                = "HIGH_CPU"
    HIGH_MEMORY             = "HIGH_MEMORY"
    HIGH_ERROR_RATE         = "HIGH_ERROR_RATE"
    # Predictivas (tendencias)
    DISK_EXHAUSTION_PREDICTED = "DISK_EXHAUSTION_PREDICTED"
    MEMORY_LEAK_PREDICTED   = "MEMORY_LEAK_PREDICTED"
    ERROR_RATE_ESCALATING   = "ERROR_RATE_ESCALATING"
    # SLO
    SLO_BUDGET_BURNING      = "SLO_BUDGET_BURNING"
    # Infraestructura K8s (observe_infrastructure)
    SERVICE_NO_ENDPOINTS    = "SERVICE_NO_ENDPOINTS"    # Service sin pods sanos
    LOADBALANCER_NO_IP      = "LOADBALANCER_NO_IP"      # LB sin EXTERNAL-IP
    PVC_PENDING             = "PVC_PENDING"             # PVC atascado en Pending
    PVC_MOUNT_ERROR         = "PVC_MOUNT_ERROR"         # FailedMount en eventos
    DEPLOYMENT_DEGRADED     = "DEPLOYMENT_DEGRADED"     # réplicas < deseadas
    NODE_PRESSURE           = "NODE_PRESSURE"           # DiskPressure/MemoryPressure/PIDPressure
    K8S_EVENT_WARNING       = "K8S_EVENT_WARNING"       # Warning event de infraestructura
    VAULT_SEALED            = "VAULT_SEALED"            # Vault sellado o no inicializado


class Severity(StrEnum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class SupervisorDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REPLAN = "REPLAN"


class MessageType(StrEnum):
    REQUEST  = "REQUEST"
    RESPONSE = "RESPONSE"
    EVENT    = "EVENT"
    ERROR    = "ERROR"


# ── Límites del sistema ────────────────────────────────────────────────────────
MAX_PLAN_STEPS        = 8
MAX_GRAPH_ITERATIONS  = MAX_PLAN_STEPS + 2
MAX_RETRIES_SUPERVISOR = 1
MAX_PROMPT_CHARS      = 4_000
MAX_CONTEXT_CHARS     = 12_000
MAX_ANSWER_CHARS      = 8_000
RATE_LIMIT_MAX        = 15
RATE_LIMIT_WINDOW     = 60   # segundos
