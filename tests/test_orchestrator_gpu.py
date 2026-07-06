"""Phase 1 (ROADMAP.md): GPU-aware session scheduling.

Entirely spec-assertion tests — no cluster, no GPU node, zero cloud cost.
They prove the *shape* of what would be submitted, not that a real GPU
node schedules it (that's Phase 3, once a physical node exists).
"""

import pytest
from fastapi.testclient import TestClient

import agentbox.orchestrator.k8s as k8s_module
from agentbox.config import get_settings
from agentbox.orchestrator.base import WorkloadSpec
from agentbox.orchestrator.k8s import GPU_RESOURCE_NAME, KubernetesExecutor

BASE_SPEC_KWARGS = dict(
    session_id="11111111-1111-1111-1111-111111111111",
    attempt=1,
    image="python:3.12-slim",
    command=["python", "-c", "print('hi')"],
    env={},
    cpu_limit="500m",
    memory_limit="512Mi",
    timeout_seconds=60,
)


@pytest.fixture()
def executor(monkeypatch):
    # Skip real kubeconfig discovery entirely: _build_job only touches
    # instance attributes set in __init__, never the cluster.
    monkeypatch.setattr(k8s_module.config, "load_incluster_config", lambda: None)
    return KubernetesExecutor()


# -- Job spec rendering ------------------------------------------------------


def test_non_gpu_job_has_no_gpu_resources_toleration_or_affinity(executor):
    spec = WorkloadSpec(**BASE_SPEC_KWARGS)
    job = executor._build_job("abx-test-a1", spec)

    pod_spec = job.spec.template.spec
    limits = pod_spec.containers[0].resources.limits
    assert GPU_RESOURCE_NAME not in limits
    assert pod_spec.tolerations is None
    assert pod_spec.affinity is None


def test_gpu_job_sets_resource_limit(executor):
    spec = WorkloadSpec(**BASE_SPEC_KWARGS, gpu_count=1, gpu_type="nvidia-t4")
    job = executor._build_job("abx-test-a1", spec)

    limits = job.spec.template.spec.containers[0].resources.limits
    assert limits[GPU_RESOURCE_NAME] == "1"
    # CPU/memory limits are untouched by the GPU branch.
    assert limits["cpu"] == "500m"
    assert limits["memory"] == "512Mi"


def test_gpu_job_sets_toleration_matching_configured_taint(executor):
    spec = WorkloadSpec(**BASE_SPEC_KWARGS, gpu_count=1, gpu_type="nvidia-t4")
    job = executor._build_job("abx-test-a1", spec)

    tolerations = job.spec.template.spec.tolerations
    assert len(tolerations) == 1
    assert tolerations[0].key == executor.gpu_taint_key == "agentbox.io/gpu"
    assert tolerations[0].value == executor.gpu_taint_value == "true"
    assert tolerations[0].effect == "NoSchedule"


def test_gpu_job_sets_node_affinity_matching_configured_label(executor):
    spec = WorkloadSpec(**BASE_SPEC_KWARGS, gpu_count=1, gpu_type="nvidia-t4")
    job = executor._build_job("abx-test-a1", spec)

    affinity = job.spec.template.spec.affinity
    term = affinity.node_affinity.required_during_scheduling_ignored_during_execution
    expr = term.node_selector_terms[0].match_expressions[0]
    assert expr.key == executor.gpu_node_label == "agentbox.io/node-pool"
    assert expr.operator == "In"
    assert expr.values == [executor.gpu_node_label_value] == ["gpu"]


def test_gpu_job_has_both_toleration_and_affinity(executor):
    # Toleration alone would only *permit* scheduling anywhere untainted;
    # affinity is what actually pins the pod to the GPU pool.
    spec = WorkloadSpec(**BASE_SPEC_KWARGS, gpu_count=1, gpu_type="nvidia-t4")
    job = executor._build_job("abx-test-a1", spec)

    pod_spec = job.spec.template.spec
    assert pod_spec.tolerations
    assert pod_spec.affinity


def test_submit_rejects_gpu_when_deployment_disabled(executor):
    executor.gpu_enabled = False
    spec = WorkloadSpec(**BASE_SPEC_KWARGS, gpu_count=1, gpu_type="nvidia-t4")
    with pytest.raises(ValueError, match="GPU scheduling is disabled"):
        executor.submit(spec)


# -- API-level rejection ------------------------------------------------------


@pytest.fixture()
def client(db):
    from agentbox.main import app, app_state

    class NoopExecutor:
        def cancel(self, ref):
            pass

        def logs(self, ref, tail=1000):
            return ""

    app_state["executor"] = NoopExecutor()
    return TestClient(app)


VALID = {
    "image": "python:3.12-slim",
    "command": ["python", "-c", "print('hi')"],
}


def test_gpu_request_rejected_when_gpu_disabled(client, monkeypatch):
    monkeypatch.setenv("AGENTBOX_GPU_ENABLED", "false")
    get_settings.cache_clear()
    resp = client.post(
        "/v1/sessions", json={**VALID, "gpu": {"count": 1, "type": "nvidia-t4"}}
    )
    get_settings.cache_clear()
    assert resp.status_code == 422
    assert "disabled" in resp.text


def test_gpu_request_accepted_when_gpu_enabled(client, monkeypatch):
    monkeypatch.setenv("AGENTBOX_GPU_ENABLED", "true")
    get_settings.cache_clear()
    resp = client.post(
        "/v1/sessions", json={**VALID, "gpu": {"count": 1, "type": "nvidia-t4"}}
    )
    get_settings.cache_clear()
    assert resp.status_code == 201
    body = resp.json()
    assert body["gpu_count"] == 1
    assert body["gpu_type"] == "nvidia-t4"


def test_gpu_count_of_zero_is_always_allowed(client, monkeypatch):
    monkeypatch.setenv("AGENTBOX_GPU_ENABLED", "false")
    get_settings.cache_clear()
    resp = client.post(
        "/v1/sessions", json={**VALID, "gpu": {"count": 0, "type": "nvidia-t4"}}
    )
    get_settings.cache_clear()
    assert resp.status_code == 201
    assert resp.json()["gpu_count"] == 0


def test_gpu_count_above_one_rejected(client):
    resp = client.post(
        "/v1/sessions", json={**VALID, "gpu": {"count": 2, "type": "nvidia-t4"}}
    )
    assert resp.status_code == 422


def test_non_gpu_session_defaults_to_zero(client):
    resp = client.post("/v1/sessions", json=VALID)
    assert resp.status_code == 201
    body = resp.json()
    assert body["gpu_count"] == 0
    assert body["gpu_type"] is None
