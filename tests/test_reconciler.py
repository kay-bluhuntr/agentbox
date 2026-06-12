"""Reconciler behaviour tests.

These run against a fake in-memory executor, so they exercise the real
control-loop logic (submission, retries, backoff, timeouts, TTL cleanup)
with no cluster. The database is SQLite locally and real Postgres in CI
(see conftest.py).
"""

from datetime import UTC, datetime, timedelta

import pytest

from agentbox.models import Session
from agentbox.orchestrator.base import WorkloadPhase, WorkloadSpec, WorkloadStatus
from agentbox.reconciler import Reconciler, backoff_delay
from agentbox.state import State


class FakeExecutor:
    """Scriptable executor: tests enqueue the statuses it should report."""

    def __init__(self):
        self.submitted: list[WorkloadSpec] = []
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []
        self.statuses: dict[str, WorkloadStatus] = {}
        self.fail_submit = False

    def submit(self, spec: WorkloadSpec) -> str:
        if self.fail_submit:
            raise RuntimeError("executor unavailable")
        self.submitted.append(spec)
        ref = f"job-{spec.session_id[:8]}-a{spec.attempt}"
        self.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.WAITING)
        return ref

    def status(self, ref: str) -> WorkloadStatus:
        return self.statuses.get(ref, WorkloadStatus(phase=WorkloadPhase.GONE))

    def logs(self, ref: str, tail: int = 1000) -> str:
        return ""

    def cancel(self, ref: str) -> None:
        self.cancelled.append(ref)

    def cleanup(self, ref: str) -> None:
        self.cleaned.append(ref)


@pytest.fixture()
def executor():
    return FakeExecutor()


@pytest.fixture()
def reconciler(executor, db):
    return Reconciler(executor)


def make_session(db, **overrides) -> str:
    defaults = dict(
        image="python:3.12-slim",
        command=["python", "-c", "print('hi')"],
        env={},
        cpu_limit="500m",
        memory_limit="512Mi",
        timeout_seconds=60,
        max_retries=0,
    )
    defaults.update(overrides)
    with db() as s:
        session = Session(**defaults)
        s.add(session)
        s.commit()
        return session.id


def get(db, session_id) -> Session:
    with db() as s:
        return s.get(Session, session_id)


def test_pending_session_gets_submitted(db, executor, reconciler):
    sid = make_session(db)
    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.SCHEDULED
    assert s.attempt == 1
    assert len(executor.submitted) == 1


def test_full_lifecycle_to_success(db, executor, reconciler):
    sid = make_session(db)
    reconciler.tick()  # pending -> scheduled

    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.RUNNING)
    reconciler.tick()  # scheduled -> running
    assert get(db, sid).state == State.RUNNING

    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.SUCCEEDED, exit_code=0)
    reconciler.tick()  # running -> succeeded
    s = get(db, sid)
    assert s.state == State.SUCCEEDED
    assert s.exit_code == 0
    assert s.finished_at is not None


def test_failure_with_retry_budget_schedules_retry(db, executor, reconciler):
    sid = make_session(db, max_retries=2)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(
        phase=WorkloadPhase.FAILED, exit_code=1, reason="OOMKilled"
    )

    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.RETRYING
    assert s.next_retry_at is not None
    assert s.failure_reason == "OOMKilled"


def test_retry_resubmits_after_backoff_elapses(db, executor, reconciler):
    sid = make_session(db, max_retries=2)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.FAILED, exit_code=1)
    reconciler.tick()  # -> retrying

    # Force the backoff window into the past
    with db() as s:
        sess = s.get(Session, sid)
        sess.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()

    reconciler.tick()  # retrying -> scheduled (attempt 2)
    s = get(db, sid)
    assert s.state == State.SCHEDULED
    assert s.attempt == 2
    assert len(executor.submitted) == 2


def test_exhausted_retries_is_terminal_failure(db, executor, reconciler):
    sid = make_session(db, max_retries=0)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.FAILED, exit_code=1)
    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.FAILED
    assert s.finished_at is not None


def test_running_past_deadline_times_out_and_cancels(db, executor, reconciler):
    sid = make_session(db, timeout_seconds=30)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.RUNNING)
    reconciler.tick()  # -> running

    with db() as s:
        sess = s.get(Session, sid)
        sess.started_at = datetime.now(UTC) - timedelta(seconds=120)
        s.commit()

    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.TIMED_OUT
    assert ref in executor.cancelled


def test_vanished_workload_is_treated_as_failure(db, executor, reconciler):
    sid = make_session(db, max_retries=1)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    del executor.statuses[ref]  # simulate the Job being deleted out-of-band

    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.RETRYING
    assert "disappeared" in s.failure_reason


def test_submit_error_fails_the_session(db, executor, reconciler):
    executor.fail_submit = True
    sid = make_session(db)
    reconciler.tick()
    s = get(db, sid)
    assert s.state == State.FAILED
    assert "submit error" in s.failure_reason


def test_terminal_sessions_get_cleaned_after_ttl(db, executor, reconciler):
    sid = make_session(db)
    reconciler.tick()
    ref = get(db, sid).executor_ref
    executor.statuses[ref] = WorkloadStatus(phase=WorkloadPhase.SUCCEEDED, exit_code=0)
    reconciler.tick()  # -> succeeded

    # Within TTL: untouched
    reconciler.tick()
    assert get(db, sid).cleaned_at is None

    # Past TTL: cleaned
    with db() as s:
        sess = s.get(Session, sid)
        sess.finished_at = datetime.now(UTC) - timedelta(seconds=9999)
        s.commit()
    reconciler.tick()
    s = get(db, sid)
    assert s.cleaned_at is not None
    assert ref in executor.cleaned


def test_backoff_is_bounded_and_grows():
    cap = 300.0
    for attempt in range(1, 10):
        for _ in range(50):
            d = backoff_delay(attempt, base=5.0, cap=cap)
            assert 0 <= d <= min(cap, 5.0 * (2**attempt))
