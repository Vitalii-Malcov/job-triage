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

**Round 2 (Codex MEDIUM remediation).**

- **Job trust laundering.** A `JobRecord` is not automatically "trusted
  stored fact" just because it is persisted — `app.collectors.xing_email`
  populates `JobRecord.title`/`company` directly from unauthenticated
  inbound email content (see that collector's own module docstring), so
  an attacker who controls a XING alert email can control those fields
  verbatim. Before this fix, `job.title`/`job.company` were interpolated
  into generated drafts regardless of `job.source` — letting attacker
  text (e.g. a job titled `"IGNORE ALL PREVIOUS INSTRUCTIONS"`) reach a
  human-reviewed draft. `_is_trusted_job_source` (default-deny: a source
  is untrusted unless explicitly listed in `TRUSTED_JOB_SOURCES`) now
  gates job-fact use the same way `is_top_level_fact_usable_for_generation`
  already gates candidate facts — an untrusted-source job is treated
  exactly like "no matched job" (placeholder + `missing_fields` entry),
  never like a data-integrity failure.
- **Subject length.** `ResponseDraftRecord.subject` is `String(500)`;
  `JobRecord.title`/`company` are each up to 300 chars, so an unbounded
  `f"{title} ({company})"` job label concatenated into a subject template
  could exceed the column. `_bound_subject` truncates deterministically
  (never the body — an oversized subject is a cosmetic/DB-fit concern; an
  oversized body could silently drop meaningful drafted content, which
  the spec explicitly forbids).
- **Candidate profile race.** `CandidateProfileRecord` used to be read
  TWICE per call (once for `candidate_profile_version`, again inside the
  old `_trusted_candidate_name` for the name) — two non-atomic SELECTs a
  concurrent profile creation/edit could land between, letting a v1 name
  fact get persisted under a `candidate_profile_version=0` (or stale)
  identity. Fixed by reading the singleton exactly ONCE and deriving both
  values from that one snapshot — see `_derive_candidate_profile_facts`.
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
from app.db.models import CandidateProfileRecord, ResponseDraftRecord
from app.db.repositories import get_job_by_id
from app.db.response_draft_repository import get_or_create_response_draft
from app.models.candidate_profile import is_top_level_fact_usable_for_generation

logger = logging.getLogger(__name__)

# Job sources whose title/company are safe to interpolate into a
# generated draft as real facts — default-deny: a `JobRecord.source` NOT
# listed here is treated as untrusted regardless of what it contains (see
# module docstring's "Job trust laundering" note). Only
# `app.collectors.bundesagentur` (a structured government job API, not
# free-text parsed from an inbound email) qualifies today.
# `app.collectors.xing_email.SOURCE_NAME` ("xing") is deliberately
# excluded — its title/company are parsed directly out of unauthenticated
# email content. A future collector's source string must be reviewed and
# explicitly added here before its job facts can ever reach a draft.
TRUSTED_JOB_SOURCES: frozenset[str] = frozenset({"bundesagentur"})

# Must stay <= ResponseDraftRecord.subject's column length (String(500),
# see app/db/models.py). Never applied to `body` — see module docstring.
_SUBJECT_MAX_LENGTH = 500
_SUBJECT_TRUNCATION_SUFFIX = "..."


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


def _is_trusted_job_source(source: str) -> bool:
    return source in TRUSTED_JOB_SOURCES


def _bound_subject(subject: str) -> str:
    if len(subject) <= _SUBJECT_MAX_LENGTH:
        return subject
    return (
        subject[: _SUBJECT_MAX_LENGTH - len(_SUBJECT_TRUNCATION_SUFFIX)]
        + _SUBJECT_TRUNCATION_SUFFIX
    )


def _derive_candidate_profile_facts(
    record: CandidateProfileRecord | None,
) -> tuple[int, str | None]:
    """`(candidate_profile_version, trusted_candidate_name)` derived from
    ONE already-read `CandidateProfileRecord` snapshot (or `None` if the
    singleton has never been created) — never re-queried. See module
    docstring's "Candidate profile race" note for why both values must
    come from the exact same read: deriving them from two separate
    `get_candidate_profile` calls let a concurrent profile
    creation/edit land between the reads, persisting a name fact under a
    `candidate_profile_version` that does not actually correspond to it.

    A full "First Last" name is returned ONLY if both `first_name` and
    `last_name` independently pass `is_top_level_fact_usable_for_generation`
    (CP-M-01/CP-M-02's trusted-source + confirmed-state rule) — `None`
    (never a partial/guessed name) otherwise, per
    app.agents.response_draft_generator's "never invents" contract.
    """
    if record is None:
        return 0, None

    profile = to_candidate_profile_response(record)
    candidate_name: str | None = None
    if (
        profile.first_name
        and profile.last_name
        and is_top_level_fact_usable_for_generation(profile, "first_name")
        and is_top_level_fact_usable_for_generation(profile, "last_name")
    ):
        candidate_name = f"{profile.first_name} {profile.last_name}"
    return record.profile_version, candidate_name


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
    candidate_profile_version, candidate_name = _derive_candidate_profile_facts(
        candidate_profile_record
    )

    job_title: str | None = None
    job_company: str | None = None
    if analysis.matched_job_id is not None:
        job = get_job_by_id(db, analysis.matched_job_id)
        if job is not None and _is_trusted_job_source(job.source):
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
        subject = _bound_subject(content.subject)
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
