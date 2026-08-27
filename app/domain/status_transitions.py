from app.models.application_status import ApplicationStatus

# Explicit transition table. REJECTED and WITHDRAWN are terminal: an
# application can be withdrawn or rejected from any non-terminal status, but
# once there, no further status change is allowed.
ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.NEW: frozenset(
        {
            ApplicationStatus.SAVED,
            ApplicationStatus.APPLIED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        }
    ),
    ApplicationStatus.SAVED: frozenset(
        {
            ApplicationStatus.APPLIED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        }
    ),
    ApplicationStatus.APPLIED: frozenset(
        {
            ApplicationStatus.INTERVIEW,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        }
    ),
    ApplicationStatus.INTERVIEW: frozenset(
        {
            ApplicationStatus.OFFER,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        }
    ),
    ApplicationStatus.OFFER: frozenset(
        {
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        }
    ),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.WITHDRAWN: frozenset(),
}


class InvalidStatusTransitionError(ValueError):
    """Raised when a job status change is not allowed from its current status."""

    def __init__(self, current: ApplicationStatus, target: ApplicationStatus) -> None:
        self.current = current
        self.target = target
        self.allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        allowed_desc = ", ".join(sorted(s.value for s in self.allowed)) or "none (terminal status)"
        super().__init__(
            f"Cannot transition job status from {current.value} to {target.value}. "
            f"Allowed transitions from {current.value}: {allowed_desc}."
        )


def validate_transition(current: ApplicationStatus, target: ApplicationStatus) -> None:
    """Raise InvalidStatusTransitionError if `current -> target` is not allowed."""
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidStatusTransitionError(current, target)
