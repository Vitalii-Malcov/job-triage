"""Stage 15: what one correlated inbound message may do to an application.

Pure decision table, no I/O:

| classification                  | e-mail may apply      | min. correlation |
|---------------------------------|-----------------------|------------------|
| RECEIPT_CONFIRMATION            | RECEIVED              | MEDIUM           |
| UNDER_REVIEW_NOTICE             | UNDER_REVIEW          | MEDIUM           |
| INTERVIEW_INVITATION            | INTERVIEW             | MEDIUM           |
| TECHNICAL_INTERVIEW_INVITATION  | TECHNICAL_INTERVIEW   | MEDIUM           |
| OFFER                           | -- (operator confirms)| MEDIUM (record)  |
| REJECTION                       | -- (operator confirms)| MEDIUM (record)  |
| REQUEST_FOR_INFORMATION         | -- (follow-up flag)   | MEDIUM           |
| OTHER_APPLICATION_MESSAGE       | --                    | MEDIUM           |
| UNKNOWN                         | --                    | MEDIUM           |

**E-mail never makes an application terminal (Codex S15 P1).** Inbound mail
carries no authenticated provenance in this pipeline (no SPF/DKIM/DMARC
evidence; sender, display name, subject and body are attacker-controllable),
and a public vacancy reference plus the company name are enough to reach
HIGH correlation from an unrelated sender. A REJECTION or OFFER message is
therefore only RECORDED (INBOUND_MESSAGE, status unchanged) with
`operator_confirmation_required=True`; the operator applies the outcome
through the audited CLI transition/override path. WITHDRAWN is never derived
from mail at all.

- Below MEDIUM nothing is recorded: a LOW-grade message may not even concern
  this application.
- MEDIUM+ without a lawful forward, non-terminal transition (already in that
  status, out-of-order older message, terminal application, terminal
  outcome, or a classification without a target) is recorded as an
  INBOUND_MESSAGE event -- visible in the history, status unchanged.
"""

from dataclasses import dataclass
from typing import Literal

from app.domain.application_lifecycle import TERMINAL_STATUSES, is_transition_allowed
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R

Action = Literal["TRANSITION", "RECORD_ONLY", "IGNORE"]
_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

TERMINAL_EMAIL_REQUIRES_OPERATOR_CONFIRMATION = "TERMINAL_EMAIL_REQUIRES_OPERATOR_CONFIRMATION"

# The only statuses an inbound e-mail can apply by itself -- all non-terminal.
EMAIL_TARGET_STATUS: dict[R, S] = {
    R.RECEIPT_CONFIRMATION: S.RECEIVED,
    R.UNDER_REVIEW_NOTICE: S.UNDER_REVIEW,
    R.INTERVIEW_INVITATION: S.INTERVIEW,
    R.TECHNICAL_INTERVIEW_INVITATION: S.TECHNICAL_INTERVIEW,
}
# Outcomes an e-mail may only REPORT; the operator decides.
OPERATOR_CONFIRMED_OUTCOME: dict[R, S] = {
    R.OFFER: S.OFFER,
    R.REJECTION: S.REJECTED,
}
MIN_RECORD_CORRELATION = "MEDIUM"
MIN_TRANSITION_CORRELATION = "MEDIUM"

assert not set(EMAIL_TARGET_STATUS.values()) & TERMINAL_STATUSES


@dataclass(frozen=True)
class ResponseDecision:
    action: Action
    target: S | None
    reason: str
    followup_required: bool = False
    # The status an operator is asked to confirm (OFFER / REJECTED), if any.
    proposed_status: S | None = None

    @property
    def operator_confirmation_required(self) -> bool:
        return self.proposed_status is not None


def decide_response(
    current: S,
    classification: R,
    *,
    correlation_confidence: str,
    classifier_confidence: str,
) -> ResponseDecision:
    if _RANK.get(correlation_confidence, 0) < _RANK[MIN_RECORD_CORRELATION]:
        return ResponseDecision("IGNORE", None, "CORRELATION_TOO_WEAK")
    outcome = OPERATOR_CONFIRMED_OUTCOME.get(classification)
    if outcome is not None:
        if current in TERMINAL_STATUSES:
            return ResponseDecision("RECORD_ONLY", None, "APPLICATION_TERMINAL")
        return ResponseDecision(
            "RECORD_ONLY",
            None,
            TERMINAL_EMAIL_REQUIRES_OPERATOR_CONFIRMATION,
            proposed_status=outcome,
        )
    followup = classification == R.REQUEST_FOR_INFORMATION
    target = EMAIL_TARGET_STATUS.get(classification)
    if target is None:
        return ResponseDecision(
            "RECORD_ONLY", None, f"NO_STATUS_FOR_{classification.value}", followup
        )
    if target == current:
        return ResponseDecision("RECORD_ONLY", None, "ALREADY_IN_STATUS")
    if current in TERMINAL_STATUSES:
        return ResponseDecision("RECORD_ONLY", None, "APPLICATION_TERMINAL")
    if not is_transition_allowed(current, target):
        return ResponseDecision("RECORD_ONLY", None, "NOT_ALLOWED_FROM_CURRENT_STATUS")
    if _RANK.get(correlation_confidence, 0) < _RANK[MIN_TRANSITION_CORRELATION]:
        return ResponseDecision("RECORD_ONLY", None, "INSUFFICIENT_EVIDENCE_FOR_TRANSITION")
    return ResponseDecision("TRANSITION", target, f"{classification.value}_TO_{target.value}")
