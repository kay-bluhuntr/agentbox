from agentbox.config import get_settings
from agentbox.orchestrator.base import Executor, LocalExecutor


def build_executor() -> Executor:
    settings = get_settings()
    if settings.executor == "local":
        return LocalExecutor()
    from agentbox.orchestrator.k8s import KubernetesExecutor

    return KubernetesExecutor()
