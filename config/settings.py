"""
Configuración centralizada de Amael-AgenticIA.

Todas las variables de entorno de la plataforma se definen aquí
usando Pydantic Settings. Un único punto de verdad para la config.

Uso:
    from config.settings import settings
    url = settings.ollama_base_url
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Configuración de la plataforma leída desde variables de entorno.

    Los valores por defecto corresponden al entorno Kubernetes en-cluster
    (namespace amael-ia). Para desarrollo local, crear un archivo .env.
    """

    # ── LLM (Ollama — única GPU disponible) ───────────────────────────────────
    ollama_base_url: str = Field(
        default="http://ollama-service:11434",
        alias="OLLAMA_BASE_URL",
    )
    llm_provider: str = Field(
        default="ollama",
        alias="LLM_PROVIDER",
    )
    embed_provider: str = Field(
        default="ollama",
        alias="EMBED_PROVIDER",
    )
    embed_api_key: str | None = Field(default=None, alias="EMBED_API_KEY")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str = Field(
        default="qwen2.5:14b",
        alias="LLM_MODEL",
    )
    llm_vision_model: str = Field(
        default="qwen2.5-vl:7b",
        alias="LLM_VISION_MODEL",
    )
    llm_embed_model: str = Field(
        default="nomic-embed-text",
        alias="LLM_EMBED_MODEL",
    )

    # ── Servicios internos ────────────────────────────────────────────────────
    k8s_agent_url: str = Field(
        default="http://k8s-agent-service:8002",
        alias="K8S_AGENT_URL",
    )
    productivity_service_url: str = Field(
        default="http://productivity-service:8001",
        alias="PRODUCTIVITY_SERVICE_URL",
    )
    whatsapp_bridge_url: str = Field(
        default="http://whatsapp-bridge-service:3000",
        alias="WHATSAPP_BRIDGE_URL",
    )
    whatsapp_personal_url: str = Field(
        default="http://whatsapp-personal-service:3001",
        alias="WHATSAPP_PERSONAL_URL",
    )
    piper_service_url: str = Field(
        default="http://piper-service:8010",
        alias="PIPER_SERVICE_URL",
    )

    # ── Seguridad ─────────────────────────────────────────────────────────────
    internal_api_secret: str = Field(alias="INTERNAL_API_SECRET")
    jwt_secret_key: str = Field(alias="JWT_SECRET_KEY")
    jwt_algorithm: str = "HS256"
    session_secret_key: str = Field(alias="SESSION_SECRET_KEY")

    # ── OAuth (Google) ────────────────────────────────────────────────────────
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="https://amael-ia.richardx.dev/auth/callback",
        alias="GOOGLE_REDIRECT_URI",
    )

    # Control de acceso gestionado en PostgreSQL (user_profile + user_identities)
    # El admin_phone se usa en el day-planner para notificaciones WhatsApp
    admin_phone: str = Field(default="521XXXXXXXXXX", alias="ADMIN_PHONE")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = Field(default="postgres-service", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="amael", alias="POSTGRES_DB")
    postgres_user: str = Field(default="amael", alias="POSTGRES_USER")
    postgres_password: str = Field(alias="POSTGRES_PASSWORD")
    postgres_pool_min: int = Field(default=2, alias="POSTGRES_POOL_MIN")
    postgres_pool_max: int = Field(default=10, alias="POSTGRES_POOL_MAX")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_host: str = Field(default="redis-service", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_url: str = Field(default="http://qdrant-service:6333", alias="QDRANT_URL")
    qdrant_api_key: str | None = Field(default=None, alias="QDRANT_API_KEY")

    # ── MinIO ─────────────────────────────────────────────────────────────────
    minio_endpoint: str = Field(default="minio-service:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_bucket: str = Field(default="amael-uploads", alias="MINIO_BUCKET")

    # ── Observabilidad ────────────────────────────────────────────────────────
    otel_endpoint: str = Field(
        default="http://otel-collector.observability.svc.cluster.local:4317",
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    otel_service_name: str = Field(default="amael-backend", alias="OTEL_SERVICE_NAME")
    prometheus_port: int = Field(default=8000, alias="PROMETHEUS_PORT")

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_max: int = Field(default=15, alias="RATE_LIMIT_MAX")
    rate_limit_window: int = Field(default=60, alias="RATE_LIMIT_WINDOW")

    # ── Agente SRE (k8s-agent) ────────────────────────────────────────────────
    sre_loop_interval_seconds: int = Field(default=60, alias="SRE_LOOP_INTERVAL")
    sre_confidence_threshold: float = Field(default=0.7, alias="SRE_CONFIDENCE_THRESHOLD")
    sre_max_restarts_per_hour: int = Field(default=3, alias="SRE_MAX_RESTARTS_PER_HOUR")
    sre_circuit_breaker_threshold: int = Field(default=5, alias="SRE_CB_THRESHOLD")
    sre_circuit_breaker_reset: int = Field(default=300, alias="SRE_CB_RESET_SECONDS")
    sre_prometheus_url: str = Field(
        default="http://kube-prometheus-stack-prometheus.observability:9090",
        alias="PROMETHEUS_URL",
    )
    sre_namespace: str = Field(default="amael-ia", alias="SRE_NAMESPACE")
    sre_lease_name: str = Field(default="sre-agent-leader", alias="SRE_LEASE_NAME")

    # ── Vault ─────────────────────────────────────────────────────────────────
    vault_addr: str = Field(
        default="http://vault.vault.svc.cluster.local:8200",
        alias="VAULT_ADDR",
    )
    vault_role: str = Field(default="amael-productivity", alias="VAULT_ROLE")

    # ── Gabriel — GitHub defaults ─────────────────────────────────────────────
    github_default_owner: str = Field(default="", alias="GABRIEL_GITHUB_OWNER")
    github_default_repo:  str = Field(default="", alias="GABRIEL_GITHUB_REPO")
    github_default_branch: str = Field(default="main", alias="GABRIEL_GITHUB_BRANCH")

    # ── Entorno ───────────────────────────────────────────────────────────────
    environment: str = Field(default="production", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    # ── Validadores de seguridad ──────────────────────────────────────────────

    @field_validator("jwt_secret_key", "internal_api_secret", "session_secret_key")
    @classmethod
    def validate_secret_length(cls, v: str, info) -> str:
        if len(v) < 28:
            raise ValueError(
                f"{info.field_name} debe tener al menos 28 caracteres "
                f"(actual: {len(v)}). Genera uno con: openssl rand -hex 32"
            )
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_not_default(cls, v: str) -> str:
        insecure = {"dev-secret", "secret", "changeme", "password", "jwt-secret"}
        if v.lower() in insecure:
            raise ValueError("JWT_SECRET_KEY usa un valor inseguro conocido")
        return v

    # ── Propiedades derivadas ─────────────────────────────────────────────────

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level debe ser uno de {valid}")
        return upper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Retorna la instancia singleton de Settings.
    Usa lru_cache para que solo se lea el entorno una vez.

    En tests, invalida el cache con get_settings.cache_clear()
    para inyectar configuración diferente.
    """
    return Settings()


# Instancia global — importar directamente en el código
settings = get_settings()
