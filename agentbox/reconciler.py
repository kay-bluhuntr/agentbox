"""The reconciliation loop.

This is the heart of the system. Kubernetes Jobs are level-triggered and
unreliable as a source of truth (Jobs get deleted, nodes vanish, the API
server hiccups), so the reconciler continuously compares desired state in
Postgres against observed state in the executor and converges:

- PENDING sessions get submitted.
- SCHEDULED/RUNNING sessions get their executor status folded back in.
- Sessions past their deadline are cancelled and marked TIMED_OUT.
- FAILED sessions with retry budget move to RETRYING with exponential
  backoff (with jitter), then back to SCHEDULED for resubmission.
- Terminal sessions older than the TTL get their cluster resources deleted
  — the same pattern I used to stop ephemeral-environment sprawl across
  100+ namespaces at Nordcloud, applied here per-session.

Leader election uses a Postgres advisory lock: replicas race for the lock
and the losers idle, so running 2+ control-plane pods is safe without any
extra infrastructure.
"""

import random
import threading
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, text

from agentbox.config import get_settings
from agentbox.models import Session, get_engine, get_session_factory
from agentbox.observability import (
    RECONCILE_ERRORS,
    RECONCILE_LOOPS,
    SESSION_RETRIES,
    SESSIONS_FINISHED,
)
from agentbox.orchestrator.base import Executor, WorkloadPhase, WorkloadSpec
from agentbox.state import State, is_terminal, transition

log = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite (used in tests) drops tzinfo; normalise to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter (AWS-style)."""
    return random.uniform(0, min(cap, base * (2**attempt)))


class Reconciler:
    def __init__(self, executor: Executor):
        self.executor = executor
        self.settings = get_settings()
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def run_forever(self) -> None:
        engine = get_engine()
        with engine.connect() as lock_conn:
            # Leader election needs a shared lock service; Postgres advisory
            # locks give us one for free. On any other backend (SQLite in
            # dev/tests) there's no second replica to race, so lead directly.
            if engine.dialect.name == "postgresql":
                self._wait_for_leadership(lock_conn)
                if self._stop.is_set():
                    return
            log.info("reconciler_leading")
            while not self._stop.wait(self.settings.reconcile_interval_seconds):
                try:
                    self.tick()
                    RECONCILE_LOOPS.inc()
                except Exception:
                    RECONCILE_ERRORS.inc()
                    log.exception("reconcile_tick_failed")

    def _wait_for_leadership(self, lock_conn) -> None:
        """Block until this replica wins the Postgres advisory lock.

        The lock is session-scoped: if the leader dies, its connection drops
        and the lock releases, so a standby takes over within one poll.
        """
        acquired = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": self.settings.reconciler_lock_key},
        ).scalar()
        if acquired:
            return
        log.info("reconciler_standby", reason="another replica holds the lock")
        while not self._stop.wait(5.0):
            acquired = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": self.settings.reconciler_lock_key},
            ).scalar()
            if acquired:
                return

    def stop(self) -> None:
        self._stop.set()

    # -- one pass -------------------------------------------------------------

    def tick(self) -> None:
        now = _utcnow()
        factory = get_session_factory()
        with factory() as db:
            sessions = db.scalars(
                select(Session).where(Session.cleaned_at.is_(None))
            ).all()
            for s in sessions:
                self._reconcile_one(db, s, now)
            db.commit()

    def _reconcile_one(self, db, s: Session, now: datetime) -> None:
        state = State(s.state)

        if state == State.PENDING:
            self._submit(s)
        elif state in (State.SCHEDULED, State.RUNNING):
            self._observe(s, now)
        elif state == State.RETRYING:
            if s.next_retry_at is None or now >= _aware(s.next_retry_at):
                self._submit(s)
        elif is_terminal(state):
            self._maybe_cleanup(s, now)

    # -- behaviours -----------------------------------------------------------

    def _submit(self, s: Session) -> None:
        s.attempt += 1
        spec = WorkloadSpec(
            session_id=s.id,
            attempt=s.attempt,
            image=s.image,
            command=list(s.command),
            env=dict(s.env),
            cpu_limit=s.cpu_limit,
            memory_limit=s.memory_limit,
            timeout_seconds=s.timeout_seconds,
        )
        try:
            s.executor_ref = self.executor.submit(spec)
            s.state = transition(State(s.state), State.SCHEDULED)
            log.info("session_scheduled", session_id=s.id, attempt=s.attempt)
        except Exception as exc:
            s.state = transition(State(s.state), State.FAILED)
            s.failure_reason = f"submit error: {exc}"
            s.finished_at = _utcnow()
            SESSIONS_FINISHED.labels(state=State.FAILED).inc()
            log.exception("session_submit_failed", session_id=s.id)

    def _observe(self, s: Session, now: datetime) -> None:
        # Soft timeout first: it's our contract with the caller.
        if State(s.state) == State.RUNNING and s.deadline_exceeded(now):
            self._timeout(s, now)
            return

        status = self.executor.status(s.executor_ref)

        if status.phase == WorkloadPhase.RUNNING and State(s.state) == State.SCHEDULED:
            s.state = transition(State.SCHEDULED, State.RUNNING)
            s.started_at = now
            log.info("session_running", session_id=s.id)
        elif status.phase == WorkloadPhase.SUCCEEDED:
            s.state = transition(State(s.state), State.SUCCEEDED)
            s.exit_code = status.exit_code
            s.finished_at = now
            SESSIONS_FINISHED.labels(state=State.SUCCEEDED).inc()
            log.info("session_succeeded", session_id=s.id, attempt=s.attempt)
        elif status.phase == WorkloadPhase.FAILED:
            self._fail(s, now, status.reason or "workload failed", status.exit_code)
        elif status.phase == WorkloadPhase.GONE:
            # The Job vanished beneath us (deleted, node loss). Treat as failure
            # so the retry budget decides what happens next.
            self._fail(s, now, "workload disappeared from executor", None)

    def _fail(self, s: Session, now: datetime, reason: str, exit_code: int | None) -> None:
        s.state = transition(State(s.state), State.FAILED)
        s.failure_reason = reason
        s.exit_code = exit_code
        s.finished_at = now

        if s.attempt <= s.max_retries:
            delay = backoff_delay(
                s.attempt,
                self.settings.retry_backoff_base_seconds,
                self.settings.retry_backoff_cap_seconds,
            )
            s.state = transition(State.FAILED, State.RETRYING)
            s.next_retry_at = now + timedelta(seconds=delay)
            s.finished_at = None
            SESSION_RETRIES.inc()
            log.info(
                "session_retrying",
                session_id=s.id,
                attempt=s.attempt,
                delay_seconds=round(delay, 1),
                reason=reason,
            )
        else:
            SESSIONS_FINISHED.labels(state=State.FAILED).inc()
            log.info("session_failed", session_id=s.id, reason=reason)

    def _timeout(self, s: Session, now: datetime) -> None:
        try:
            self.executor.cancel(s.executor_ref)
        except Exception:
            log.exception("cancel_failed", session_id=s.id)
        s.state = transition(State(s.state), State.TIMED_OUT)
        s.failure_reason = f"exceeded timeout of {s.timeout_seconds}s"
        s.finished_at = now
        SESSIONS_FINISHED.labels(state=State.TIMED_OUT).inc()
        log.info("session_timed_out", session_id=s.id)

    def _maybe_cleanup(self, s: Session, now: datetime) -> None:
        finished_at = _aware(s.finished_at)
        if finished_at is None:
            s.cleaned_at = now
            return
        if (now - finished_at).total_seconds() < self.settings.terminal_ttl_seconds:
            return
        if s.executor_ref:
            try:
                self.executor.cleanup(s.executor_ref)
            except Exception:
                log.exception("cleanup_failed", session_id=s.id)
                return  # retry next tick
        s.cleaned_at = now
        log.info("session_cleaned", session_id=s.id)
