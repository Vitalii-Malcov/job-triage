"""Stage 7C orchestration: turns an existing Stage 7B
`GmailMessageAnalysisRecord` into a persisted `ResponseDraftRecord`
proposal, using app.agents.response_draft_generator's deterministic,
template-based content generation.

**Hard boundary (spec-mandated, enforced by construction — not just
documented).** This module, and everything it calls, never:

- sends email, creates a Gmail draft, replies, or forwards;
- marks a mailbox message read/unread, or moves/archives/deletes one;
- opens a URL or fetches anything referenced in email content;
- mutates `JobRecord.status` / `ApplicationStatus`, or any other
  `JobRecord` column — every DB access here is a pure SELECT plus one
  INSERT into `response_drafts`;
- contacts a recruiter, invokes browser automation, or sends any
  generated draft anywhere;
- calls Telegram or any other external notifier;
- calls an LLM or any other external provider of any kind — generation
  is a pure, deterministic, offline template lookup (see
  app.agents.response_draft_generator's own module docstring for why).

**7C reuses 7B, it does not re-derive matching/classification (spec
requirement).** This module never calls `app.services.email_matching` or
`app.agents.email_classifier` directly — it only reads the LATEST
already-persisted `GmailMessageAnalysisRecord` for the requested message
via `app.db.gmail_analysis_repository.get_latest_analysis_for_message`.
A message that has never been analyzed (POST
/gmail/messages/{id}/analyze not yet called) cannot get a response
draft — see `ResponseDraftAnalysisNotFoundError`.

**Trust boundary (spec requirement).** Generated draft text is built
exclusively from already-trusted stored facts — see
app.agents.response_draft_generator's own module docstring for the full
rationale. The ONLY email-derived input this module passes to the
generator at all is the message's own `subject`/`body_plain`, and only
to pick a DE/EN template SET via
`app.agents.response_draft_generator.detect_language` — never as text
that reaches the generated draft itself.

**`requires_human_review` is always `True`.** Every `ResponseDraftRecord`
this module writes carries `requires_human_review=True` unconditionally
(see that model's docstring) — Stage 7C only ever proposes; Stage 7D
(not yet built) owns human approval and any eventual send action.
"""

import logging

from sqlalchemy.orm import Session

from app.agents.response_draft_generator import (
    RESPONSE_DRAFT_GENERATOR_VERSION,
    RESPONSE_DRAFT_PROVIDER,
    detect_language,
    generate_response_draft,
)
from app.db.candidate_profile_repository import (
    get_candidate_profile,
    to_candidate_profile_response,
)
from app.db.gmail_analysis_repository import get_latest_analysis_for_message
from app.db.gmail_repository import get_message_by_id
from app.db.models import ResponseDraftRecord
from app.db.repositories import get_job_by_id
from app.db.response_draft_repository import get_or_create_response_draft
from app.models.candidate_profile import is_top_level_fact_usable_for_generation

logger = logging.getLogger(__name__)


class ResponseDraftMessageNotFoundError(Exception):
    """Raised when no `gmail_messages` row exists for
    (account_key, gmail_message_id) — mapped to 404 by app/api/routes.py,
    mirrors app.services.gmail_message_analysis.GmailMessageNotFoundError.
    """


class ResponseDraftAnalysisNotFoundError(Exception):
    """Raised when the requested message has never been analyzed (no
    `GmailMessageAnalysisRecord` yet — POST /gmail/messages/{id}/analyze
    must run first). Mapped to 409 by app/api/routes.py: the message
    exists, but the precondition for drafting a response does not.
    """


def _trusted_candidate_name(db: Session) -> str | None:
    """A full "First Last" name, but ONLY if both `first_name` and
    `last_name` independently pass `is_top_level_fact_usable_for_generation`
    (see app.models.candidate_profile — CP-M-01/CP-M-02's trusted-source +
    confirmed-state rule). Returns `None` (never a partial/guessed name)
    if either field is missing or not provenance-confirmed — see
    app.agents.response_draft_generator's "never invents" contract.
    """
    record = get_candidate_profile(db)
    if record is None:
        return None
    profile = to_candidate_profile_response(record)
    if not profile.first_name or not profile.last_name:
        return None
    if not is_top_level_fact_usable_for_generation(profile, "first_name"):
        return None
    if not is_top_level_fact_usable_for_generation(profile, "last_name"):
        return None
    return f"{profile.first_name} {profile.last_name}"


def generate_response_draft_for_message(
    db: Session, account_key: str, gmail_message_id: int
) -> tuple[ResponseDraftRecord, bool]:
    """Generate (or idempotently re-fetch) one Stage 7C response-draft
    revision for an already-analyzed, account-scoped Gmail message.
    Returns `(record, created)` — `created=False` when a draft already
    exists for this exact (message, analysis revision, candidate profile
    version, generator version) identity, in which case the pre-existing
    row is returned unmodified.
    """
    message = get_message_by_id(db, account_key, gmail_message_id)
    if message is None:
        raise ResponseDraftMessageNotFoundError(
            f"No gmail_messages row for account_key={account_key!r} id={gmail_message_id!r}"
        )

    analysis = get_latest_analysis_for_message(db, account_key, gmail_message_id)
    if analysis is None:
        raise ResponseDraftAnalysisNotFoundError(
            f"gmail_message_id={gmail_message_id!r} has not been analyzed yet "
            "(POST /gmail/messages/{id}/analyze must run first)."
        )

    candidate_profile_record = get_candidate_profile(db)
    candidate_profile_version = (
        candidate_profile_record.profile_version if candidate_profile_record is not None else 0
    )
    candidate_name = _trusted_candidate_name(db)

    job_title: str | None = None
    job_company: str | None = None
    if analysis.matched_job_id is not None:
        job = get_job_by_id(db, analysis.matched_job_id)
        if job is not None:
            job_title = job.title
            job_company = job.company

    language = detect_language(message.subject, message.body_plain)
    content = generate_response_draft(
        classification=analysis.classification,
        language=language,
        candidate_name=candidate_name,
        job_title=job_title,
        job_company=job_company,
    )

    if content is None:
        status = "NO_RESPONSE_RECOMMENDED"
        reason = (
            f"No automated response is recommended for classification '{analysis.classification}'."
        )
        subject = body = language_value = None
        missing_fields: tuple[str, ...] = ()
    else:
        status = "PROPOSED"
        reason = None
        subject = content.subject
        body = content.body
        language_value = content.language
        missing_fields = content.missing_fields

    record, created = get_or_create_response_draft(
        db,
        account_key=account_key,
        gmail_message_id=gmail_message_id,
        analysis_id=analysis.id,
        analysis_version=analysis.analysis_version,
        candidate_profile_version=candidate_profile_version,
        matched_job_id=analysis.matched_job_id,
        classification=analysis.classification,
        status=status,
        reason=reason,
        subject=subject,
        body=body,
        language=language_value,
        missing_fields=missing_fields,
        provider=RESPONSE_DRAFT_PROVIDER,
        generator_version=RESPONSE_DRAFT_GENERATOR_VERSION,
    )

    # Privacy (mirrors app.services.gmail_message_analysis): counts/enums/
    # ids only, never subject/body/addresses/generated draft content.
    logger.info(
        "response_draft_generated gmail_message_id=%s created=%s status=%s classification=%s",
        gmail_message_id,
        created,
        record.status,
        record.classification,
    )
    return record, created
