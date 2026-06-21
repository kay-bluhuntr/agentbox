"""Application configuration, sourced from environment variables.

Every setting has a sane local default so `make dev` works out of the box.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service
    service_name: str = "agentbox"
    log_level: str = "INFO"

    # Database. This default is a local-dev convenience only — real deployments
    # override it via AGENTBOX_DATABASE_URL, sourced from a Kubernetes Secret
    # (Helm: database.existingSecret; prod: ExternalSecrets -> Secrets Manager).
    database_url: str = "postgresql+psycopg://agentbox:agentbox@localhost:5433/agentbox"

    # Executor: "kubernetes" in real deployments, "local" for laptop dev without a cluster
    executor: str = "kubernetes"

    # Kubernetes execution
    exec_namespace: str = "agentbox-exec"
    exec_service_account: str = "agentbox-runner"
    kubeconfig: str | None = None  # None = in-cluster config

    # Guardrails applied to every workload
    default_cpu_limit: str = "500m"
    default_memory_limit: str = "512Mi"
    max_timeout_seconds: int = 3600
    default_timeout_seconds: int = 300
    max_retries_ceiling: int = 5

    # Reconciler
    reconcile_interval_seconds: float = 2.0
    retry_backoff_base_seconds: float = 5.0
    retry_backoff_cap_seconds: float = 300.0
    terminal_ttl_seconds: int = 900  # how long finished sessions keep their k8s resources

    # Postgres advisory lock key so only one reconciler runs across replicas
    reconciler_lock_key: int = 815001

    model_config = {"env_prefix": "AGENTBOX_"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
