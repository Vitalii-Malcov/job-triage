"""Stage 7E orchestration: detect when a tracked APPLIED job's Gmail
correspondence may warrant a follow-up, and generate a deterministic,
persisted, auditable follow-up PROPOSAL — never a send.

**Hard boundary (mirrors app.services.response_draft's module docstring
exactly, one stage further downstream).** This module, and everything it
calls, never:

- sends email, creates a Gmail draft, replies, or forwards — see
  app/services/follow_up_send.py for the ONLY code path in this stage
  that may eventually transmit anything, and only once a
  `FollowUpApprovalRecord` with `decision == "APPROVED"` exists;
- marks a mailbox message read/unread, or moves/archives/deletes one;
- opens a URL or fetches anything referenced in email content;
- mutates `JobRecord.status` / `ApplicationStatus`, or any other
  `JobRecord` column — every DB access here is a pure SELECT plus one
  INSERT into `follow_up_proposals`;
- calls Telegram or any other external notifier;
- calls an LLM or any other external provider — generation is a pure,
  deterministic, offline template lookup (see
  app.agents.follow_up_generator's own module docstring).

**No background scheduler/cron (spec requirement).** `list_due_follow_ups`
performs one bounded, synchronous scan when explicitly invoked (see
app/api/routes.py's `POST /follow-ups/evaluate`) — nothing in this module
runs on a timer.

**Never infers application age from `JobRecord.first_seen_at`/
`last_seen_at` (CLAUDE.md hard requirement).** Eligibility is entirely
driven by real Gmail correspondence timestamps — see
app.services.follow_up_eligibility's own module docstring.

**Trust boundary (mirrors app.services.response_draft's own).** The
`JobRecord.source` trust gate for job title/company is REUSED verbatim
from Stage 7C (`app.services.response_draft.TRUSTED_JOB_SOURCES`) rather
than duplicated — an untrusted-source job (e.g. `xing`, parsed directly
from unauthenticated inbound email content) is treated exactly like "no
matched job" here too, never like a data-integrity failure. The anchor
message's own `subject`/`body_plain` is used ONLY to pick a DE/EN
template set (`app.agents.response_draft_generator.detect_language`,
reused as-is) — never as text that reaches the generated follow-up
itself.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.agents.follow_up_generator import (
    FOLLOW_UP_GENERATOR_VERSION,
    FOLLOW_UP_PROVIDER,
    generate_follow_up_content,
)
from app.agents.response_draft_generator import detect_language
from app.core.config import Settings, get_settings
from app.db.candidate_profile_repository import get_candidate_profile, to_candidate_profile_response
from app.db.follow_up_repository import (
    get_matched_thread_ids_for_job,
    get_or_create_follow_up_proposal,
    get_thread_message_infos,
    to_follow_up_proposal,
)
from app.db.gmail_repository import get_message_by_id
from app.db.models import CandidateProfileRecord, FollowUpProposalRecord
from app.db.repositories import get_job_by_id, list_jobs
from app.models.application_status import ApplicationStatus
from app.models.candidate_profile import is_top_level_fact_usable_for_generation
from app.models.follow_up import FollowUpEvaluationResult, FollowUpScanSummary
from app.services.follow_up_eligibility import evaluate_follow_up_eligibility
from app.services.response_draft import TRUSTED_JOB_SOURCES

logger = logging.getLogger(__name__)

# Bounded scan of tracked APPLIED jobs per POST /follow-ups/evaluate call
# (spec: "Bound: candidate jobs/applications considered" — same ethos as
# app.services.email_matching.MATCH_CANDIDATE_SCAN_LIMIT). No background
# scheduler exists (spec requirement); a larger backlog requires multiple
# manual calls (offset-paginated via app.db.repositories.list_jobs).
FOLLOW_UP_JOB_SCAN_LIMIT = 200

# Must stay <= FollowUpProposalRecord.subject's column length (String(500)).
_SUBJECT_MAX_LENGTH = 500
_SUBJECT_TRUNCATION_SUFFIX = "..."


class FollowUpJobNotFoundError(Exception):
    """Raised when no `jobs` row exists for `job_id` — mapped to 404 by
    app/api/routes.py.
    """


class FollowUpRepositoryInconsistentAnchorError(Exception):
    """See `_build_proposal` — should be unreachable in practice: the
    eligibility engine only ever names an anchor drawn from messages this
    same account_key/thread was just read from.
    """


def _bound_subject(subject: str) -> str:
    if len(subject) <= _SUBJECT_MAX_LENGTH:
        return subject
    return (
        subject[: _SUBJECT_MAX_LENGTH - len(_SUBJECT_TRUNCATION_SUFFIX)]
        + _SUBJECT_TRUNCATION_SUFFIX
    )


def _is_trusted_job_source(source: str) -> bool:
    return source in TRUSTED_JOB_SOURCES


def _derive_candidate_name(record: CandidateProfileRecord | None) -> str | None:
    """A full "First Last" name, ONLY if both `first_name` and `last_name`
    independently pass `is_top_level_fact_usable_for_generation` — `None`
    (never a partial/guessed name) otherwise. Mirrors
    app.services.response_draft._derive_candidate_profile_facts's own
    single-read-derives-both-values discipline, simplified here since this
    stage has no revision identity that also needs the profile version.
    """
    if record is None:
        return None
    profile = to_candidate_profile_response(record)
    if (
        profile.first_name
        and profile.last_name
        and is_top_level_fact_usable_for_generation(profile, "first_name")
        and is_top_level_fact_usable_for_generation(profile, "last_name")
    ):
        return f"{profile.first_name} {profile.last_name}"
    return None


def _build_proposal(
    db: Session,
    *,
    account_key: str,
    job,
    thread_id: int,
    anchor_gmail_message_id: int,
    eligibility_reason: str,
    due_at: datetime,
) -> tuple[FollowUpProposalRecord, bool]:
    anchor_message = get_message_by_id(db, account_key, anchor_gmail_message_id)
    # Should be unreachable: the eligibility engine only ever names an
    # anchor drawn from messages this same account_key/thread just read
    # (see app.db.follow_up_repository.get_thread_message_infos). Fail
    # loudly rather than silently generating a fact-free draft.
    if anchor_message is None:
        raise FollowUpRepositoryInconsistentAnchorError(
            f"anchor_gmail_message_id={anchor_gmail_message_id!r} could not be resolved "
            f"for account_key={account_key!r}"
        )

    candidate_profile_record = get_candidate_profile(db)
    candidate_name = _derive_candidate_name(candidate_profile_record)

    job_title: str | None = None
    job_company: str | None = None
    if _is_trusted_job_source(job.source):
        job_title = job.title
        job_company = job.company

    language = detect_language(anchor_message.subject, anchor_message.body_plain)
    content = generate_follow_up_content(
        language=language,
        candidate_name=candidate_name,
        job_title=job_title,
        job_company=job_company,
    )

    return get_or_create_follow_up_proposal(
        db,
        account_key=account_key,
        job_id=job.id,
        gmail_thread_id=thread_id,
        anchor_gmail_message_id=anchor_gmail_message_id,
        eligibility_reason=eligibility_reason,
        due_at=due_at,
        subject=_bound_subject(content.subject),
        body=content.body,
        language=content.language,
        missing_fields=content.missing_fields,
        provider=FOLLOW_UP_PROVIDER,
        generator_version=FOLLOW_UP_GENERATOR_VERSION,
    )


def evaluate_follow_up_for_job(
    db: Session,
    account_key: str,
    job_id: int,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> FollowUpEvaluationResult:
    """Evaluate one job's follow-up eligibility, persisting (or
    idempotently re-fetching) a `FollowUpProposalRecord` when, and only
    when, eligible. Raises `FollowUpJobNotFoundError` if no such job
    exists.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        raise FollowUpJobNotFoundError(f"No jobs row for job_id={job_id!r}")

    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    matched_thread_ids = get_matched_thread_ids_for_job(db, account_key, job_id)
    thread_messages = []
    thread_id: int | None = None
    if len(matched_thread_ids) == 1:
        thread_id = next(iter(matched_thread_ids))
        thread_messages = get_thread_message_infos(db, account_key, thread_id)

    result = evaluate_follow_up_eligibility(
        job_status=job.status,
        matched_thread_count=len(matched_thread_ids),
        thread_messages=thread_messages,
        follow_up_delay=timedelta(days=settings.follow_up_delay_days),
        now=now,
    )

    if result.eligibility != "ELIGIBLE" or result.anchor_gmail_message_id is None:
        return FollowUpEvaluationResult(
            job_id=job_id,
            eligibility=result.eligibility,
            reason=result.reason,
            anchor_gmail_message_id=result.anchor_gmail_message_id,
            due_at=result.due_at,
            proposal=None,
            created=None,
        )

    assert thread_id is not None  # noqa: S101 - ELIGIBLE implies exactly one matched thread
    record, created = _build_proposal(
        db,
        account_key=account_key,
        job=job,
        thread_id=thread_id,
        anchor_gmail_message_id=result.anchor_gmail_message_id,
        eligibility_reason=result.reason,
        due_at=result.due_at,
    )

    logger.info(
        "follow_up_evaluated job_id=%s eligibility=%s created=%s proposal_id=%s",
        job_id,
        result.eligibility,
        created,
        record.id,
    )
    return FollowUpEvaluationResult(
        job_id=job_id,
        eligibility="ELIGIBLE",
        reason=result.reason,
        anchor_gmail_message_id=result.anchor_gmail_message_id,
        due_at=result.due_at,
        proposal=to_follow_up_proposal(record),
        created=created,
    )


def list_due_follow_ups(
    db: Session,
    account_key: str,
    *,
    limit: int = FOLLOW_UP_JOB_SCAN_LIMIT,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> FollowUpScanSummary:
    """Bounded, manually-triggered scan across tracked APPLIED jobs (spec:
    "no background scheduler/cron" — this must be called explicitly, see
    `POST /follow-ups/evaluate`). Evaluates every scanned job and creates
    a proposal for each newly-eligible one; re-running this is always
    idempotent (see `FollowUpProposalRecord`'s docstring).
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    jobs = list_jobs(db, status=ApplicationStatus.APPLIED, limit=limit, offset=0)
    results = [
        evaluate_follow_up_for_job(db, account_key, job.id, settings=settings, now=now)
        for job in jobs
    ]

    eligible = sum(1 for r in results if r.eligibility == "ELIGIBLE")
    proposals_created = sum(1 for r in results if r.created is True)
    return FollowUpScanSummary(
        scanned=len(results),
        eligible=eligible,
        proposals_created=proposals_created,
        results=results,
    )
