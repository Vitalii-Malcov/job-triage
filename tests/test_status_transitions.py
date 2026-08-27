import pytest

from app.domain.status_transitions import (
    ALLOWED_TRANSITIONS,
    InvalidStatusTransitionError,
    validate_transition,
)
from app.models.application_status import ApplicationStatus as S

VALID_TRANSITIONS = [
    (S.NEW, S.SAVED),
    (S.NEW, S.APPLIED),
    (S.NEW, S.REJECTED),
    (S.NEW, S.WITHDRAWN),
    (S.SAVED, S.APPLIED),
    (S.SAVED, S.REJECTED),
    (S.SAVED, S.WITHDRAWN),
    (S.APPLIED, S.INTERVIEW),
    (S.APPLIED, S.REJECTED),
    (S.APPLIED, S.WITHDRAWN),
    (S.INTERVIEW, S.OFFER),
    (S.INTERVIEW, S.REJECTED),
    (S.INTERVIEW, S.WITHDRAWN),
    (S.OFFER, S.WITHDRAWN),
    (S.OFFER, S.REJECTED),
]

INVALID_TRANSITIONS = [
    (S.NEW, S.INTERVIEW),
    (S.NEW, S.OFFER),
    (S.NEW, S.NEW),
    (S.SAVED, S.NEW),
    (S.SAVED, S.INTERVIEW),
    (S.SAVED, S.OFFER),
    (S.SAVED, S.SAVED),
    (S.APPLIED, S.NEW),
    (S.APPLIED, S.SAVED),
    (S.APPLIED, S.OFFER),
    (S.APPLIED, S.APPLIED),
    (S.INTERVIEW, S.NEW),
    (S.INTERVIEW, S.SAVED),
    (S.INTERVIEW, S.APPLIED),
    (S.INTERVIEW, S.INTERVIEW),
    (S.OFFER, S.NEW),
    (S.OFFER, S.SAVED),
    (S.OFFER, S.APPLIED),
    (S.OFFER, S.INTERVIEW),
    (S.OFFER, S.OFFER),
    (S.REJECTED, S.NEW),
    (S.REJECTED, S.SAVED),
    (S.REJECTED, S.APPLIED),
    (S.REJECTED, S.INTERVIEW),
    (S.REJECTED, S.OFFER),
    (S.REJECTED, S.WITHDRAWN),
    (S.REJECTED, S.REJECTED),
    (S.WITHDRAWN, S.NEW),
    (S.WITHDRAWN, S.SAVED),
    (S.WITHDRAWN, S.APPLIED),
    (S.WITHDRAWN, S.INTERVIEW),
    (S.WITHDRAWN, S.OFFER),
    (S.WITHDRAWN, S.REJECTED),
    (S.WITHDRAWN, S.WITHDRAWN),
]


def test_transition_table_covers_every_status():
    assert set(ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize("current,target", VALID_TRANSITIONS)
def test_valid_transitions_are_accepted(current, target):
    validate_transition(current, target)  # must not raise


@pytest.mark.parametrize("current,target", INVALID_TRANSITIONS)
def test_invalid_transitions_are_rejected(current, target):
    with pytest.raises(InvalidStatusTransitionError):
        validate_transition(current, target)


def test_terminal_statuses_have_no_outgoing_transitions():
    assert ALLOWED_TRANSITIONS[S.REJECTED] == frozenset()
    assert ALLOWED_TRANSITIONS[S.WITHDRAWN] == frozenset()


def test_error_message_lists_allowed_targets():
    with pytest.raises(InvalidStatusTransitionError) as exc:
        validate_transition(S.APPLIED, S.SAVED)
    message = str(exc.value)
    assert "APPLIED" in message
    assert "SAVED" in message
    assert "INTERVIEW" in message
    assert "REJECTED" in message
    assert "WITHDRAWN" in message


def test_error_message_for_terminal_status_says_none():
    with pytest.raises(InvalidStatusTransitionError) as exc:
        validate_transition(S.REJECTED, S.APPLIED)
    assert "none (terminal status)" in str(exc.value)


def test_every_valid_and_invalid_pair_is_exhaustive_for_all_status_combinations():
    all_pairs = {(s1, s2) for s1 in S for s2 in S}
    covered_pairs = set(VALID_TRANSITIONS) | set(INVALID_TRANSITIONS)
    assert covered_pairs == all_pairs
