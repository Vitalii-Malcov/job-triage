"""Stage 15: what one correlated inbound message may do to an application.

Pure decision table, no I/O:

| classification                  | target status        | min. correlation |
|---------------------------------|----------------------|------------------|
| RECEIPT_CONFIRMATION            | RECEIVED             | MEDIUM           |
| UNDER_REVIEW_NOTICE             | UNDER_REVIEW         | MEDIUM           |
| INTERVIEW_INVITATION            | INTERVIEW            | MEDIUM           |
| TECHNICAL_INTERVIEW_INVITATION  | TECHNICAL_INTERVIEW  | MEDIUM           |
| OFFER                           | OFFER                | HIGH             |
| REJECTION                       | REJECTED             | HIGH             |
| REQUEST_FOR_INFORMATION         | -- (follow-up flag)  | MEDIUM           |
| OTHER_APPLICATION_MESSAGE       | --                   | MEDIUM           |
| UNKNOWN                         | --                   | MEDIUM           |

- Below MEDIUM nothing is recorded at all: a LOW-grade message may not
  even concern this application.
- MEDIUM+ without a lawful forward transition (already in that status, an
  out-of-order older message, a terminal application, a terminal outcome
  with only MEDIUM evidence, or a classification with no target) is
  recorded as an INBOUND_MESSAGE event -- visible in the history, status
  unchanged.
- Terminal outcomes (OFFER, REJECTED) additionally require a HIGH classifier
  confidence; Stage 7B's REJECTION-vs-positive conflict already resolves to
  OTHER, so one ambiguous keyword can never end an application.
"""

from dataclasses import dataclass
from typing import Literal

from app.domain.application_lifecycle import TERMINAL_STATUSES, is_transition_allowed
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R

Action = Literal["TRANSITION", "RECORD_ONLY", "IGNORE"]
_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

TARGET_STATUS: dict[R, S] = {
    R.RECEIPT_CONFIRMATION: S.RECEIVED,
    R.UNDER_REVIEW_NOTICE: S.UNDER_REVIEW,
    R.INTERVIEW_INVITATION: S.INTERVIEW,
    R.TECHNICAL_INTERVIEW_INVITATION: S.TECHNICAL_INTERVIEW,
    R.OFFER: S.OFFER,
    R.REJECTION: S.REJECTED,
}
MIN_RECORD_CORRELATION = "MEDIUM"
MIN_TRANSITION_CORRELATION: dict[S, str] = {
    S.RECEIVED: "MEDIUM",
    S.UNDER_REVIEW: "MEDIUM",
    S.INTERVIEW: "MEDIUM",
    S.TECHNICAL_INTERVIEW: "MEDIUM",
    S.OFFER: "HIGH",
    S.REJECTED: "HIGH",
}


@dataclass(frozen=True)
class ResponseDecision:
    action: Action
    target: S | None
    reason: str
    followup_required: bool = False


def decide_response(
    current: S,
    classification: R,
    *,
    correlation_confidence: str,
    classifier_confidence: str,
) -> ResponseDecision:
    if _RANK.get(correlation_confidence, 0) < _RANK[MIN_RECORD_CORRELATION]:
        return ResponseDecision("IGNORE", None, "CORRELATION_TOO_WEAK")
    followup = classification == R.REQUEST_FOR_INFORMATION
    target = TARGET_STATUS.get(classification)
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
    if _RANK.get(correlation_confidence, 0) < _RANK[MIN_TRANSITION_CORRELATION[target]]:
        return ResponseDecision("RECORD_ONLY", None, "INSUFFICIENT_EVIDENCE_FOR_TRANSITION")
    if target in TERMINAL_STATUSES and classifier_confidence != "HIGH":
        return ResponseDecision("RECORD_ONLY", None, "CLASSIFIER_NOT_CONFIDENT_FOR_TERMINAL")
    return ResponseDecision("TRANSITION", target, f"{classification.value}_TO_{target.value}")
