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

**Round 3 (Blocker R3-002): semantic match confidence is NOT
correspondence trust.** A Codex review reproduced an attacker-controlled
email body — a valid `Referenz-Nr:` plus copied "Vielen Dank für Ihre
Bewerbung" acknowledgement text — resolving to `APPLICATION_RECEIVED` /
`HIGH` match confidence / `HIGH` classification confidence with
`requires_human_review=False`. Stage 7A has NO SPF/DKIM/DMARC/
Authentication-Results evidence anywhere in this pipeline (see
`app.services.email_matching`'s own module docstring on thread trust for
the same point) — `from_address`, subject, and body are all
attacker-controlled, unauthenticated strings. A deterministic matcher
finding strong TEXTUAL evidence that an email is "about" a tracked job
says nothing about who actually sent it. `determine_requires_human_review`
therefore now requires review for every message this pipeline associates
with ANY tracked job/application (`match_type != "UNMATCHED"`), regardless
of how confidently it was classified — see that function's own docstring
for the exact, narrow case where `False` is still returned.
"""

import hashlib
import logging

from sqlalchemy.orm import Session

from app.agents.email_classifier import CONSEQUENTIAL_CLASSIFICATIONS, classify_email
from app.db.gmail_analysis_repository import (
    compute_context_fingerprint,
    get_job_candidates,
    get_or_create_analysis,
    get_thread_prior_matches,
)
from app.db.gmail_repository import get_message_by_id
from app.db.models import GmailMessageAnalysisRecord
from app.services.email_matching import extract_reference_tokens, match_email_to_job

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

# Round 3 (Blocker R3-002): every classification with a real job/application
# consequence always requires review, regardless of confidence —
# CONSEQUENTIAL_CLASSIFICATIONS plus APPLICATION_RECEIVED, which a Codex
# review reproduced as the exploited gap (see module docstring). Not
# included: AUTOMATED_NOTIFICATION and UNKNOWN — both are still forced to
# require review whenever the message is associated with a tracked job at
# all, via `determine_requires_human_review`'s own `match_type !=
# "UNMATCHED"` check; only an UNMATCHED + UNKNOWN message (no job
# association AND no recognized actionable phrase) can ever return False.
_ALWAYS_REVIEW_CLASSIFICATIONS: frozenset[str] = CONSEQUENTIAL_CLASSIFICATIONS | frozenset(
    {"APPLICATION_RECEIVED"}
)


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
    section, hardened by Round 3 Blocker R3-002 — see module docstring).

    **`requires_human_review=False` exact semantics.** It means, and ONLY
    means: this message could not be associated with any tracked
    job/application AND contains no recognized actionable phrase — i.e.
    it is irrelevant noise as far as this pipeline can tell. It never
    means "the sender is trusted", "safe to act on", or "safe to ignore".
    Classification/match confidence may inform DISPLAY PRIORITY once a
    human is looking at flagged mail; they are never authorization to
    suppress review for correspondence this pipeline believes concerns a
    real job or application.

    Ordered checks:

    - `AMBIGUOUS` matches always require review (no arbitrary winner).
    - Either confidence dimension being LOW always requires review.
    - A consequential classification (OFFER/INTERVIEW_*/REJECTION/
      WITHDRAWAL_OR_POSITION_CLOSED/REQUEST_FOR_INFORMATION/
      GENERAL_RECRUITER_MESSAGE/APPLICATION_RECEIVED/OTHER — see
      `_ALWAYS_REVIEW_CLASSIFICATIONS`) always requires review, regardless
      of confidence.
    - Any message associated with a tracked job/application at all
      (`match_type != "UNMATCHED"`) always requires review (R3-002): this
      pipeline has no cryptographic sender authentication, so a confident
      SEMANTIC match is never grounds to treat inbound correspondence
      about that job/application as safe to leave unreviewed —
      classification may still determine priority once reviewed, never
      whether review happens at all.
    - `UNMATCHED` with any classification other than UNKNOWN (i.e. an
      "actionable" classification with nowhere to route it) requires
      review.
    - Otherwise (UNMATCHED + UNKNOWN, i.e. no job/application association
      AND no recognized actionable phrase — genuinely non-actionable,
      no-consequence noise): review is not required.
    """
    if match_type == "AMBIGUOUS":
        return True
    if match_confidence == "LOW":
        return True
    if classification_confidence == "LOW":
        return True
    if classification in _ALWAYS_REVIEW_CLASSIFICATIONS:
        return True
    if match_type != "UNMATCHED":
        return True
    if classification != "UNKNOWN":
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

    # 7B-007: extract the email's own reference tokens FIRST so
    # get_job_candidates can run its targeted lookup alongside the
    # recency-bounded pool — an exact reference match older than the
    # recency window must still be discoverable (see that function's
    # docstring).
    reference_tokens = extract_reference_tokens(f"{message.subject}\n{message.body_plain}")
    job_candidates = get_job_candidates(db, reference_tokens=reference_tokens)
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
    # 7B-003/004: makes the identity sensitive to the EFFECTIVE candidate
    # pool and thread context actually considered — see
    # compute_context_fingerprint's own docstring for why this exists.
    context_fingerprint = compute_context_fingerprint(job_candidates, thread_prior_matches)

    record, created = get_or_create_analysis(
        db,
        account_key=account_key,
        gmail_message_id=message.id,
        analysis_version=ANALYSIS_VERSION,
        input_fingerprint=input_fingerprint,
        context_fingerprint=context_fingerprint,
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
