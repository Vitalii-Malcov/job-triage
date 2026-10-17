"""Stage 15: the application lifecycle state machine (pure, no I/O).

Two phases share one status vocabulary:

- **Preparation** (DISCOVERED -> MATCHED -> PACKAGE_READY -> APPROVED ->
  SUBMITTED) is strictly sequential: each step is a deliberate local action.
- **Employer response** (SUBMITTED -> RECEIVED -> UNDER_REVIEW -> INTERVIEW ->
  TECHNICAL_INTERVIEW -> OFFER) is monotonic but may SKIP steps: employers
  often invite to an interview without ever sending a receipt, so any later
  response state is reachable from any earlier one. Moving backwards is
  never allowed (a late receipt e-mail after an interview invitation is
  out-of-order correspondence, not a regression).
- REJECTED and WITHDRAWN are reachable from every non-terminal status.

**Terminal: OFFER, REJECTED, WITHDRAWN.** OFFER is terminal for Stage 15:
accepting/declining an offer is a separate lifecycle (Stage 16 backlog). A
genuinely needed exit from a terminal state (e.g. an offer rescinded, a
mis-recorded rejection) is an operator override -- see
`app.services.application_tracker.ApplicationTracker.override_status` --
which is recorded as its own STATUS_OVERRIDDEN event and never rewrites
history.
"""

from app.models.application_lifecycle import ApplicationLifecycleStatus as S

PREPARATION_SEQUENCE: tuple[S, ...] = (
    S.DISCOVERED,
    S.MATCHED,
    S.PACKAGE_READY,
    S.APPROVED,
    S.SUBMITTED,
)
RESPONSE_SEQUENCE: tuple[S, ...] = (
    S.SUBMITTED,
    S.RECEIVED,
    S.UNDER_REVIEW,
    S.INTERVIEW,
    S.TECHNICAL_INTERVIEW,
    S.OFFER,
)
TERMINAL_STATUSES: frozenset[S] = frozenset({S.OFFER, S.REJECTED, S.WITHDRAWN})
# Statuses at which the application has actually reached the employer.
POST_SUBMISSION_STATUSES: frozenset[S] = frozenset(RESPONSE_SEQUENCE) | {S.REJECTED}


def _build_table() -> dict[S, frozenset[S]]:
    table: dict[S, set[S]] = {status: set() for status in S}
    for current, following in zip(PREPARATION_SEQUENCE, PREPARATION_SEQUENCE[1:], strict=False):
        table[current].add(following)
    for index, current in enumerate(RESPONSE_SEQUENCE):
        table[current].update(RESPONSE_SEQUENCE[index + 1 :])
    for status in S:
        if status not in TERMINAL_STATUSES:
            table[status].update({S.REJECTED, S.WITHDRAWN})
    return {status: frozenset(targets) for status, targets in table.items()}


ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = _build_table()


class InvalidApplicationTransitionError(ValueError):
    """`current -> target` is not in the transition table."""

    def __init__(self, current: S, target: S) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(ALLOWED_TRANSITIONS[current])) or "none (terminal status)"
        super().__init__(
            f"Cannot move application from {current} to {target}. Allowed from {current}: "
            f"{allowed}."
        )


def is_terminal(status: S) -> bool:
    return status in TERMINAL_STATUSES


def is_transition_allowed(current: S, target: S) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def validate_transition(current: S, target: S) -> None:
    if not is_transition_allowed(current, target):
        raise InvalidApplicationTransitionError(current, target)


def response_rank(status: S) -> int | None:
    """Position on the employer-response ladder, None outside it."""
    try:
        return RESPONSE_SEQUENCE.index(status)
    except ValueError:
        return None
