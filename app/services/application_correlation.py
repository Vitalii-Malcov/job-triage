"""Stage 15: correlate one inbound message to at most one application.

**Inbound mail is unauthenticated.** Sender address, display name, subject
and body are attacker-controllable strings; this pipeline has no
SPF/DKIM/DMARC evidence (same finding as Stage 7B, see
app/services/gmail_message_analysis.py). Correlation is therefore graded
by how many INDEPENDENT signals agree, and the response policy
(app/domain/application_response_policy.py) demands the strongest grade
before anything terminal (OFFER/REJECTED) is recorded.

Signals, per candidate application (only applications already submitted,
and only for messages inside the application's response window):

- DOMAIN: the sender's domain is (a subdomain of) one of the
  application's confirmed `employer_domains`.
- COMPANY: the normalized company name appears in the display name,
  subject or body.
- ROLE: the normalized role title (gender suffix such as "(m/w/d)"
  removed) appears in the subject or body.
- REFERENCE: the application's `source_reference` (e.g. the BA reference)
  appears verbatim; `external_job_id` counts only when labelled
  ("Stellen-ID 962", "Job-ID: 962", "Kennziffer 962", ...).
- THREAD: the message replies to a message already correlated to this
  application (supporting only -- thread headers are forgeable).

Grades:

- HIGH: DOMAIN + (ROLE or REFERENCE), or REFERENCE + COMPANY.
- MEDIUM: DOMAIN + (COMPANY or THREAD), or COMPANY + ROLE.
- LOW: anything else with at least one signal.

The best grade wins only if exactly one application holds it with the
highest signal count; otherwise the result is AMBIGUOUS (never a guess).
"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.agents.application_response_classifier import normalize_body, normalize_subject
from app.db.datetime_utils import ensure_utc
from app.models.application_lifecycle import Application
from app.services.email_matching import extract_sender_domain, normalize_company_name

CorrelationConfidence = Literal["HIGH", "MEDIUM", "LOW", "NONE"]
CorrelationStatus = Literal["MATCHED", "AMBIGUOUS", "UNMATCHED"]
Signal = Literal["DOMAIN", "COMPANY", "ROLE", "REFERENCE", "THREAD"]

# A response may legitimately be dated slightly before our recorded
# submission time (clock skew, Date header set by the employer's system).
RESPONSE_WINDOW_SKEW = timedelta(hours=1)
RESPONSE_WINDOW = timedelta(days=365)
_RANK: dict[CorrelationConfidence, int] = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
_GENDER_SUFFIX = re.compile(r"\(\s*[mwfdx]\s*(?:/\s*[a-z.]+\s*){1,3}\)", re.IGNORECASE)
_JOB_ID_LABEL = (
    r"(?:stellen[- ]?id|job[- ]?id|kennziffer|referenz(?:nummer)?|ref\.?|stellen(?:nummer|angebot))"
)


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InboundMessage:
    """Provider-neutral view of one inbound e-mail (live IMAP or stored)."""

    account_key: str
    message_key: str  # "mid:<message-id>" or "imap:<mailbox>:<uidvalidity>:<uid>"
    message_id_header: str | None
    reply_to_ids: tuple[str, ...]
    from_address: str | None
    from_display_name: str | None
    to_addresses: tuple[str, ...]
    subject: str
    body: str
    has_html: bool
    occurred_at: datetime | None
    direction: str = "INBOUND"

    @property
    def correlation_key(self) -> str:
        return "gmail:" + stable_hash(self.account_key, self.message_key)

    @property
    def message_id_hash(self) -> str | None:
        return stable_hash(_norm_id(self.message_id_header)) if self.message_id_header else None

    @property
    def reply_to_hashes(self) -> frozenset[str]:
        return frozenset(stable_hash(_norm_id(i)) for i in self.reply_to_ids if i)


def _norm_id(message_id: str) -> str:
    return message_id.strip().strip("<>").strip().casefold()


def message_key_for(
    message_id_header: str | None, mailbox: str, uid_validity: int, uid: int
) -> str:
    if message_id_header and _norm_id(message_id_header):
        return "mid:" + _norm_id(message_id_header)
    return f"imap:{mailbox}:{uid_validity}:{uid}"


def normalize_occurred_at(*candidates: datetime | None) -> datetime | None:
    """First usable timestamp, as UTC. A naive value is treated as UTC (the
    project's storage convention); anything else unusable is skipped."""
    for value in candidates:
        if isinstance(value, datetime):
            try:
                return ensure_utc(value).astimezone(UTC)
            except (OverflowError, ValueError):
                continue
    return None


@dataclass(frozen=True)
class CandidateEvaluation:
    application_id: int
    confidence: CorrelationConfidence
    signals: tuple[Signal, ...]


@dataclass(frozen=True)
class CorrelationResult:
    status: CorrelationStatus
    application_id: int | None
    confidence: CorrelationConfidence
    signals: tuple[Signal, ...]
    reason: str
    candidates: tuple[CandidateEvaluation, ...] = ()


def _contains_phrase(haystack: str, phrase: str) -> bool:
    phrase = phrase.strip()
    if len(phrase) < 3:
        return False
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", haystack) is not None


def role_phrase(role_title: str) -> str:
    return re.sub(r"\s+", " ", _GENDER_SUFFIX.sub(" ", role_title)).strip().casefold()


def _domain_matches(sender_domain: str | None, domains: Iterable[str]) -> bool:
    if not sender_domain:
        return False
    return any(sender_domain == d or sender_domain.endswith("." + d) for d in domains if d)


def _grade(signals: set[Signal]) -> CorrelationConfidence:
    if "DOMAIN" in signals and ({"ROLE", "REFERENCE"} & signals):
        return "HIGH"
    if {"REFERENCE", "COMPANY"} <= signals:
        return "HIGH"
    if "DOMAIN" in signals and ({"COMPANY", "THREAD"} & signals):
        return "MEDIUM"
    if {"COMPANY", "ROLE"} <= signals:
        return "MEDIUM"
    return "LOW" if signals else "NONE"


def in_response_window(application: Application, occurred_at: datetime | None) -> bool:
    if occurred_at is None or application.submitted_at is None:
        return False
    start = application.submitted_at - RESPONSE_WINDOW_SKEW
    return start <= occurred_at <= application.submitted_at + RESPONSE_WINDOW


def evaluate_candidate(
    message: InboundMessage,
    application: Application,
    *,
    thread_hashes: frozenset[str] = frozenset(),
) -> CandidateEvaluation:
    subject = normalize_subject(message.subject).casefold()
    body = normalize_body(message.body, has_html=message.has_html).casefold()
    text = f"{subject}\n{body}"
    display = (message.from_display_name or "").casefold()
    signals: set[Signal] = set()
    if _domain_matches(extract_sender_domain(message.from_address), application.employer_domains):
        signals.add("DOMAIN")
    company = normalize_company_name(application.company_name)
    normalized_text = normalize_company_name(text)
    if company and (
        _contains_phrase(normalized_text, company)
        or _contains_phrase(normalize_company_name(display), company)
    ):
        signals.add("COMPANY")
    role = role_phrase(application.role_title)
    if role and _contains_phrase(re.sub(r"\s+", " ", _GENDER_SUFFIX.sub(" ", text)), role):
        signals.add("ROLE")
    reference = (application.source_reference or "").casefold()
    if reference and len(reference) >= 6 and reference in text:
        signals.add("REFERENCE")
    external = (application.external_job_id or "").casefold()
    if external and re.search(
        _JOB_ID_LABEL + r"\W{0,3}" + re.escape(external) + r"(?!\w)", text, re.IGNORECASE
    ):
        signals.add("REFERENCE")
    if thread_hashes and message.reply_to_hashes & thread_hashes:
        signals.add("THREAD")
    ordered = tuple(s for s in ("DOMAIN", "COMPANY", "ROLE", "REFERENCE", "THREAD") if s in signals)
    return CandidateEvaluation(application.id, _grade(signals), ordered)


def correlate_message(
    message: InboundMessage,
    applications: Iterable[Application],
    *,
    applicant_addresses: Iterable[str] = (),
    thread_hashes_by_application: Mapping[int, frozenset[str]] | None = None,
) -> CorrelationResult:
    own = {a.strip().casefold() for a in applicant_addresses if a}
    if message.direction != "INBOUND":
        return CorrelationResult("UNMATCHED", None, "NONE", (), "OUTBOUND_MESSAGE")
    if (message.from_address or "").strip().casefold() in own:
        return CorrelationResult("UNMATCHED", None, "NONE", (), "OWN_MESSAGE")
    if message.occurred_at is None:
        return CorrelationResult("UNMATCHED", None, "NONE", (), "NO_TRUSTED_TIMESTAMP")
    threads = thread_hashes_by_application or {}
    evaluations = [
        evaluate_candidate(message, app, thread_hashes=threads.get(app.id, frozenset()))
        for app in applications
        if in_response_window(app, message.occurred_at)
    ]
    evaluations = [e for e in evaluations if e.confidence != "NONE"]
    if not evaluations:
        return CorrelationResult("UNMATCHED", None, "NONE", (), "NO_SIGNAL")
    best_rank = max(_RANK[e.confidence] for e in evaluations)
    top = [e for e in evaluations if _RANK[e.confidence] == best_rank]
    most = max(len(e.signals) for e in top)
    winners = [e for e in top if len(e.signals) == most]
    if len(winners) > 1:
        return CorrelationResult(
            "AMBIGUOUS",
            None,
            winners[0].confidence,
            (),
            "MULTIPLE_APPLICATIONS_EQUALLY_LIKELY",
            tuple(sorted(winners, key=lambda e: e.application_id)),
        )
    winner = winners[0]
    return CorrelationResult(
        "MATCHED",
        winner.application_id,
        winner.confidence,
        winner.signals,
        f"CORRELATED_{winner.confidence}",
        tuple(sorted(evaluations, key=lambda e: e.application_id)),
    )
