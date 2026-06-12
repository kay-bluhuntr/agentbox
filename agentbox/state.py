"""Session lifecycle state machine.

A session is one request to execute a workload. The state machine is the
single source of truth for what transitions are legal; everything else
(API, reconciler, executor) goes through `transition()` so an illegal move
is a loud error rather than silent state corruption.

    PENDING ──► SCHEDULED ──► RUNNING ──► SUCCEEDED
                   │             │
                   │             ├──► FAILED ──► RETRYING ──► SCHEDULED
                   │             ├──► TIMED_OUT
                   ▼             ▼
                CANCELLED     CANCELLED
"""

from enum import StrEnum


class State(StrEnum):
    PENDING = "pending"        # accepted, not yet submitted to the executor
    SCHEDULED = "scheduled"    # submitted; waiting for the workload to start
    RUNNING = "running"        # workload observed running
    RETRYING = "retrying"      # failed, waiting out backoff before resubmission
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({State.SUCCEEDED, State.FAILED, State.TIMED_OUT, State.CANCELLED})

_ALLOWED: dict[State, frozenset[State]] = {
    State.PENDING: frozenset({State.SCHEDULED, State.CANCELLED, State.FAILED}),
    # SCHEDULED can jump straight to SUCCEEDED/FAILED: short workloads finish
    # between reconcile ticks, so RUNNING is never observed.
    State.SCHEDULED: frozenset(
        {State.RUNNING, State.SUCCEEDED, State.FAILED, State.TIMED_OUT, State.CANCELLED}
    ),
    State.RUNNING: frozenset({State.SUCCEEDED, State.FAILED, State.TIMED_OUT, State.CANCELLED}),
    State.RETRYING: frozenset({State.SCHEDULED, State.CANCELLED, State.FAILED}),
    State.FAILED: frozenset({State.RETRYING}),
    State.SUCCEEDED: frozenset(),
    State.TIMED_OUT: frozenset(),
    State.CANCELLED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: State, target: State):
        self.current = current
        self.target = target
        super().__init__(f"illegal transition: {current} -> {target}")


def transition(current: State, target: State) -> State:
    """Validate and return the new state, raising IllegalTransition otherwise."""
    if target not in _ALLOWED[current]:
        raise IllegalTransition(current, target)
    return target


def can_transition(current: State, target: State) -> bool:
    return target in _ALLOWED[current]


def is_terminal(state: State) -> bool:
    return state in TERMINAL_STATES
