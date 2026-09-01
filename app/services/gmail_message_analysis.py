"""Stage 7B orchestration: ties app.agents.email_classifier +
app.services.email_matching + app.db.gmail_analysis_repository together
into one analysis run over an already-persisted `GmailMessageRecord`.

**Hard boundary (spec-mandated, enforced by construction — not just
documented).** This module, and everything it calls, never:

- sends email, creates a Gmail draft, replies, or forwards;
- marks a mailbox message read/unread, or moves/archives/deletes one;
- opens a URL, fetches a remote image, or follows any link found in
  email content (app.agents.email_classifier / app.services.email_matching
  only ever `re.search`/`urlparse` text that is already in memory —
  neither module makes an HTTP request, and neither is ever handed a
  requests/httpx client);
- mutates `JobRecord.status` / `ApplicationStatus`, or any other
  `JobRecord` column — `get_job_candidates`
  (app/db/gmail_analysis_repository.py) is a pure SELECT;
- contacts a recruiter, invokes browser automation, uploads a CV, or
  submits an application;
- calls Telegram or any other external notifier;
- calls an LLM or any other external provider of any kind — matching and
  classification are both pure, deterministic, offline functions (see
  their own module docstrings for why).

`requires_human_review=False` on a persisted result is never
authorization for anything downstream to act automatically — it is
purely an informational signal that this reading had strong deterministic
evidence. Every consequential classification (OFFER, INTERVIEW_*,
REJECTION, WITHDRAWAL_OR_POSITION_CLOSED) is force-flagged regardless of
confidence — see `determine_requires_human_review`.
"""

import hashlib
import logging

from sqlalchemy.orm import Session

from app.agents.email_classifier import CONSEQUENTIAL_CLASSIFICATIONS, classify_email
from app.db.gmail_analysis_repository import (
    get_job_candidates,
    get_or_create_analysis,
    get_thread_prior_matches,
)
from app.db.gmail_repository import get_message_by_id
from app.db.models import GmailMessageAnalysisRecord
from app.services.email_matching import match_email_to_job

logger = logging.getLogger(__name__)

# Bumped whenever the matching/classification ALGORITHM changes in a way
# that should produce a new analysis revision for previously-analyzed
# messages, rather than being silently masked by the idempotency check
# (spec: "Do not silently overwrite an old analysis result if
# algorithm/version changes... create a new revision/version").
ANALYSIS_VERSION = 1

# ASCII unit separator — never appears in normal email text, so it can't
# be confused with a field's own content when concatenated for hashing.
_FINGERPRINT_FIELD_SEPARATOR = "\x1f"


class GmailMessageNotFoundError(Exception):
    """Raised by `analyze_gmail_message` when no `gmail_messages` row
    exists for (account_key, gmail_message_id) — the route layer maps
    this to 404 (see app/api/routes.py's existing Gmail message lookup
    for the same pattern).
    """


def compute_input_fingerprint(subject: str, from_address: str | None, body_plain: str) -> str:
    """SHA-256 hex digest over exactly the message-own fields the
    classifier/matcher actually read. See
    `app.db.models.GmailMessageAnalysisRecord`'s `input_fingerprint`
    docstring for exactly what this deliberately excludes (thread
    context, the candidate JobRecord pool) and why.
    """
    payload = _FINGERPRINT_FIELD_SEPARATOR.join((subject, from_address or "", body_plain))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def determine_requires_human_review(
    *,
    match_type: str,
    match_confidence: str,
    classification: str,
    classification_confidence: str,
) -> bool:
    """Safe-by-default human-review gate (spec "HUMAN REVIEW FLAG"
    section) — True unless the match is decisive AND non-consequential:

    - `AMBIGUOUS` matches always require review (no arbitrary winner).
    - Either confidence dimension being LOW always requires review.
    - A consequential classification (OFFER/INTERVIEW_*/REJECTION/
      WITHDRAWAL_OR_POSITION_CLOSED/OTHER — see
      app.agents.email_classifier.CONSEQUENTIAL_CLASSIFICATIONS) always
      requires review, regardless of confidence — "consequential
      correspondence should remain visible to the user even if
      confidence is high".
    - `UNMATCHED` with any classification other than UNKNOWN (i.e. an
      "actionable" classification with nowhere to route it) requires
      review.
    """
    if match_type == "AMBIGUOUS":
        return True
    if match_confidence == "LOW":
        return True
    if classification_confidence == "LOW":
        return True
    if classification in CONSEQUENTIAL_CLASSIFICATIONS:
        return True
    if match_type == "UNMATCHED" and classification != "UNKNOWN":
        return True
    return False


def analyze_gmail_message(
    db: Session, account_key: str, gmail_message_id: int
) -> tuple[GmailMessageAnalysisRecord, bool]:
    """Run (or idempotently re-fetch) one Stage 7B analysis for an
    already-persisted, account-scoped Gmail message. Returns
    `(record, created)` — `created=False` when an analysis already exists
    for this exact (message, ANALYSIS_VERSION, input_fingerprint)
    identity, in which case the pre-existing (unmodified) row is
    returned. Raises `GmailMessageNotFoundError` if no such message
    exists for this account.
    """
    message = get_message_by_id(db, account_key, gmail_message_id)
    if message is None:
        raise GmailMessageNotFoundError(
            f"No gmail_messages row for account_key={account_key!r} id={gmail_message_id!r}"
        )

    job_candidates = get_job_candidates(db)
    thread_prior_matches = get_thread_prior_matches(
        db,
        account_key=account_key,
        thread_id=message.thread_id,
        exclude_gmail_message_id=message.id,
    )

    match_result = match_email_to_job(
        subject=message.subject,
        body_plain=message.body_plain,
        from_address=message.from_address,
        job_candidates=job_candidates,
        thread_prior_matches=thread_prior_matches,
    )
    classification_result = classify_email(
        message.subject, message.body_plain, message.from_address
    )

    requires_human_review = determine_requires_human_review(
        match_type=match_result.match_type,
        match_confidence=match_result.confidence,
        classification=classification_result.category,
        classification_confidence=classification_result.confidence,
    )

    input_fingerprint = compute_input_fingerprint(
        message.subject, message.from_address, message.body_plain
    )

    record, created = get_or_create_analysis(
        db,
        account_key=account_key,
        gmail_message_id=message.id,
        analysis_version=ANALYSIS_VERSION,
        input_fingerprint=input_fingerprint,
        match_result=match_result,
        classification_category=classification_result.category,
        classification_confidence=classification_result.confidence,
        classification_evidence=classification_result.evidence,
        is_automated=classification_result.is_automated,
        requires_human_review=requires_human_review,
    )

    # Privacy (mirrors app.services.gmail_inbox): counts/enums/ids only,
    # never subject/body/addresses.
    logger.info(
        "gmail_message_analysis_run gmail_message_id=%s created=%s match_type=%s "
        "match_confidence=%s classification=%s classification_confidence=%s "
        "requires_human_review=%s",
        message.id,
        created,
        record.match_type,
        record.match_confidence,
        record.classification,
        record.classification_confidence,
        record.requires_human_review,
    )
    return record, created
