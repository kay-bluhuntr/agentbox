"""API contract tests: validation guardrails, idempotency, error codes."""

import pytest
from fastapi.testclient import TestClient

from agentbox.state import State


@pytest.fixture()
def client(db):
    # Build the app without the lifespan (no reconciler thread in API tests)
    from agentbox.main import app, app_state

    class NoopExecutor:
        def cancel(self, ref):  # pragma: no cover - exercised via cancel test
            pass

        def logs(self, ref, tail=1000):
            return "hello from workload"

    app_state["executor"] = NoopExecutor()
    return TestClient(app)


VALID = {
    "image": "python:3.12-slim",
    "command": ["python", "-c", "print('hi')"],
}


def test_create_returns_pending_session(client):
    resp = client.post("/v1/sessions", json=VALID)
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == State.PENDING
    assert body["attempt"] == 0
    assert body["timeout_seconds"] > 0  # default applied


def test_timeout_is_clamped_to_ceiling(client):
    resp = client.post("/v1/sessions", json={**VALID, "timeout_seconds": 10_000_000})
    assert resp.status_code == 201
    assert resp.json()["timeout_seconds"] <= 3600


def test_excessive_retries_rejected(client):
    resp = client.post("/v1/sessions", json={**VALID, "max_retries": 99})
    assert resp.status_code == 422


def test_empty_command_rejected(client):
    resp = client.post("/v1/sessions", json={**VALID, "command": []})
    assert resp.status_code == 422


def test_get_unknown_session_404(client):
    assert client.get("/v1/sessions/does-not-exist").status_code == 404


def test_cancel_pending_session(client):
    sid = client.post("/v1/sessions", json=VALID).json()["id"]
    resp = client.delete(f"/v1/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["state"] == State.CANCELLED


def test_cancel_is_idempotent(client):
    sid = client.post("/v1/sessions", json=VALID).json()["id"]
    client.delete(f"/v1/sessions/{sid}")
    resp = client.delete(f"/v1/sessions/{sid}")  # second cancel: no-op, not an error
    assert resp.status_code == 200
    assert resp.json()["state"] == State.CANCELLED


def test_list_filters_by_state(client):
    client.post("/v1/sessions", json=VALID)
    sid = client.post("/v1/sessions", json=VALID).json()["id"]
    client.delete(f"/v1/sessions/{sid}")

    pending = client.get("/v1/sessions", params={"state": "pending"}).json()
    cancelled = client.get("/v1/sessions", params={"state": "cancelled"}).json()
    assert all(s["state"] == "pending" for s in pending)
    assert [s["id"] for s in cancelled] == [sid]


def test_logs_for_session_without_workload_is_empty(client):
    sid = client.post("/v1/sessions", json=VALID).json()["id"]
    resp = client.get(f"/v1/sessions/{sid}/logs")
    assert resp.status_code == 200
    assert resp.json()["logs"] == ""


def test_healthz(client):
    assert client.get("/healthz").status_code == 200
