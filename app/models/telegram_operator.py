"""Stage 16: Telegram operator-control vocabulary.

The Telegram bot is the OPERATOR's control surface for Stage 15
applications. A decision recorded here is a local workflow fact:

- APPROVE -- the operator approves this exact review package for the NEXT
  controlled step. The application moves PACKAGE_READY -> APPROVED through
  the Stage 15 `ApplicationTracker`. Nothing is submitted, sent, drafted or
  posted anywhere: a future submission stage keeps its own explicit gate.
- SKIP -- the operator declines this vacancy/package. Never a lifecycle
  status: SKIP is not REJECTED (an employer decision) and not WITHDRAWN.
- LATER -- non-final: remember the item for later review.

DETAILS is an interaction (a read), never a durable decision.

Every value is a closed vocabulary; the database enforces the same sets
with CHECK constraints (see `app.db.models.OPERATOR_DECISION_CHECKS`).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OperatorDecision(StrEnum):
    APPROVE = "APPROVE"
    SKIP = "SKIP"
    LATER = "LATER"


FINAL_DECISIONS: frozenset[OperatorDecision] = frozenset(
    {OperatorDecision.APPROVE, OperatorDecision.SKIP}
)


class OperatorAction(StrEnum):
    """A button on the operator card (DETAILS changes nothing)."""

    APPROVE = "APPROVE"
    SKIP = "SKIP"
    DETAILS = "DETAILS"
    LATER = "LATER"


class TelegramDeliveryState(StrEnum):
    """Delivery state of one operator card or lifecycle notification.

    SENDING   claimed; a send attempt is (or was) in flight
    SENT      Telegram confirmed (2xx)
    FAILED    provably NOT delivered (retryable, bounded)
    UNCERTAIN delivery can be neither proven nor disproven -- never retried
              automatically (no duplicate messages)
    """

    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


# Actor names recorded on decisions and on the lifecycle event an APPROVE
# produces.
TELEGRAM_OPERATOR_ACTOR = "TELEGRAM_OPERATOR"
CLI_OPERATOR_ACTOR = "CLI_OPERATOR"


class OperatorDecisionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    application_id: int
    card_id: int | None
    review_package_id: int | None
    target_key: str
    decision: OperatorDecision
    supersedes_decision_id: int | None
    actor: str
    reason: str | None
    lifecycle_correlation_key: str | None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        return self.decision in FINAL_DECISIONS
