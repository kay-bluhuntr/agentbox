import pytest

from agentbox.state import (
    TERMINAL_STATES,
    IllegalTransition,
    State,
    can_transition,
    is_terminal,
    transition,
)


def test_happy_path():
    s = State.PENDING
    for target in (State.SCHEDULED, State.RUNNING, State.SUCCEEDED):
        s = transition(s, target)
    assert s == State.SUCCEEDED
    assert is_terminal(s)


def test_retry_path():
    s = State.PENDING
    s = transition(s, State.SCHEDULED)
    s = transition(s, State.RUNNING)
    s = transition(s, State.FAILED)
    s = transition(s, State.RETRYING)
    s = transition(s, State.SCHEDULED)
    assert s == State.SCHEDULED


def test_cannot_resurrect_terminal_states():
    for terminal in (State.SUCCEEDED, State.TIMED_OUT, State.CANCELLED):
        for target in State:
            assert not can_transition(terminal, target), f"{terminal} -> {target} should be illegal"


def test_failed_only_goes_to_retrying():
    assert can_transition(State.FAILED, State.RETRYING)
    for target in set(State) - {State.RETRYING}:
        assert not can_transition(State.FAILED, target)


def test_illegal_transition_raises_with_context():
    with pytest.raises(IllegalTransition) as exc:
        transition(State.PENDING, State.SUCCEEDED)
    assert exc.value.current == State.PENDING
    assert exc.value.target == State.SUCCEEDED


def test_cancel_allowed_from_every_non_terminal_state_except_failed():
    cancellable = {State.PENDING, State.SCHEDULED, State.RUNNING, State.RETRYING}
    for state in cancellable:
        assert can_transition(state, State.CANCELLED)
    for state in TERMINAL_STATES:
        assert not can_transition(state, State.CANCELLED)
