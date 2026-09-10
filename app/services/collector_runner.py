"""S8A-003 (Codex re-review, layering fix): the shared fetch + score +
persist logic for every job collector, plus the company-research lookup
helper — previously embedded in `app.api.routes` and reached into from
`app.services.telegram_bot` via a private cross-layer import
(`from app.api.routes import _run_bundesagentur, ...`). That direction is
backwards: a SERVICE (telegram_bot.py) depending on the API layer for
its own core logic, and — once Stage 8A's orchestrator
(`app.services.automation`) needed the SAME functions — would have
forced either duplicating this logic a third time or introducing a
circular import between `app.api.routes` and `app.services.automation`.

This module is the single, lower-layer home for that logic. All three
callers now depend on IT, never on each other:

- `app.api.routes` (`POST /collectors/bundesagentur/run`,
  `POST /collectors/xing/run`, `POST /jobs/{id}/research`,
  `POST /jobs/score`)
- `app.services.telegram_bot` (`/run bundesagentur`, `/run xing`,
  `/research <id>`)
- `app.services.automation` (Stage 8A's orchestrator)

**Hard layering rule this module upholds:** nothing under `app.services`
may import from `app.api.routes`. This module (and every other
`app.services.*` module) imports only from `app.collectors.*`,
`app.db.*`, `app.agents.*`, and sibling `app.services.*` modules —
never from `app.api.routes`. `app.api.routes` is the one layer allowed
to import FROM here (the normal, correct direction).

**Zero behavior change.** Every function below is a verbatim relocation
of what `app.api.routes` used to define privately — same scoring, same
fingerprint-based dedup (`app.db.repositories.upsert_job`), same
per-job failure isolation (`db.rollback()` + continue, never aborting
the rest of a run), same best-effort Telegram notification and
auto-research budget. Renamed from private (`_run_bundesagentur`) to
public (`run_bundesagentur`) since this is now the module's actual
public contract, not an implementation detail of routes.py.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.job_scorer import JobScorer
from app.agents.skill_extractor import extract_skills
from app.collectors.base import CollectorError, CollectorNotConfiguredError, is_configured
from app.collectors.bundesagentur import BundesagenturCollector, is_api_key_configured
from app.collectors.xing_email import XingEmailCollector
from app.db.models import JobRecord, UserProfile
from app.db.repositories import (
    get_job_by_fingerprint,
    get_job_by_id,
    get_or_create_default_profile,
    is_message_processed,
    mark_message_processed,
    profile_skills,
    upsert_job,
)
from app.db.xing_scan_progress_repository import (
    advance_xing_scan_progress,
    compute_mailbox_scope,
    get_xing_scan_progress,
)
from app.models.company_research import CompanyResearchRunResponse
from app.models.job import Job, JobScore
from app.services.company_research import CompanyResearchService
from app.services.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

__all__ = [
    "CollectorError",
    "CollectorNotConfiguredError",
    "TouchedJob",
    "run_bundesagentur",
    "run_company_research_for_job",
    "run_xing",
    "score_and_persist",
]


@dataclass(frozen=True)
class TouchedJob:
    """S8C-POOL-001 (Codex review): a cheap, in-memory record of one job
    THIS collector call actually persisted successfully — the run-local
    attribution mechanism Stage 8C uses instead of inferring "touched by
    this run" from `JobRecord.last_seen_at >= run.started_at` (which
    could also match a job independently refreshed by a concurrent
    collector run for a different account, or a manual endpoint call,
    in the same time window). Carries only cheap technical fields
    (never job content) so a caller can cheaply preselect/bound a large
    candidate set before any DB re-query — see
    `app.services.automation_shortlist.prepare_shortlist_drafts`.
    """

    job_id: int
    score: int
    status: str
    recommendation: str


def score_and_persist(
    db: Session, profile: UserProfile, job: Job
) -> tuple[JobRecord, JobScore, bool]:
    """Score a Job against the given profile and persist it.

    Shared by POST /jobs/score and every collector run below so scoring +
    deduplication logic lives in exactly one place.
    """
    result = JobScorer(profile_skills(profile)).score(job)
    record, created = upsert_job(db, job, result)
    result.is_duplicate = not created
    return record, result, created


async def run_company_research_for_job(
    db: Session, settings, job_id: int, *, force_refresh: bool
) -> CompanyResearchRunResponse | None:
    """Fetch (or reuse cached) company research for one job.

    Shared by POST /jobs/{id}/research and the Telegram control center's
    `/research <id>` command. Returns None if the job doesn't exist —
    callers translate that into their own presentation (404 vs. a chat
    message). Raises ProviderNotConfiguredError if the active provider
    needs configuration that isn't set, InvalidCompanyIdentityError if
    the job has no usable company name, or AmbiguousCompanyIdentityError
    (FR-M-01) if the job's normalized company name is shared by 2+
    distinct known-domain companies on file — no other provider failure
    propagates here, see CompanyResearchService.get_or_run's
    failure-isolation contract and CompanyResearchRunResponse's
    refresh-outcome fields.
    """
    job = get_job_by_id(db, job_id)
    if job is None:
        return None
    return await CompanyResearchService().get_or_run(db, job, settings, force_refresh=force_refresh)


async def _maybe_auto_research(
    db: Session,
    settings,
    record: JobRecord,
    result: JobScore,
    budget: dict[str, int],
    *,
    is_lease_lost: Callable[[], bool] | None = None,
) -> None:
    """Best-effort, opt-in company research for a just-persisted high-score job.

    Off by default (settings.company_research_auto_enabled) — see
    app/core/config.py. Shared by run_bundesagentur/run_xing so the
    "research automatically for APPLY-recommended jobs" rule lives in one
    place. Failures here must never affect a collector run's
    created/updated/failed counts, same best-effort contract as the
    Telegram notification block right below each call site.

    `budget` is a per-collector-run mutable counter
    (`{"remaining": settings.company_research_auto_max_per_run}`, created
    once by the caller before its loop starts) — bounds how many automatic
    research runs a single collector run can trigger regardless of how many
    APPLY jobs it produces, so a large batch can't silently fan out into an
    unbounded number of research runs. Manual triggers (POST
    /jobs/{id}/research, Telegram /research) are unaffected by this budget.

    Codex gate follow-up (Astra R4A, lease-loss MEDIUM): `is_lease_lost`
    is an OPTIONAL callback (`None` for every standalone caller -- the
    manual endpoint, Telegram bot command; only
    `app.services.automation.run_automation_cycle` ever passes one, bound
    to `heartbeat.lease_lost.is_set`). `run_automation_cycle`'s own
    between-STEP checks (`_raise_if_lease_lost`) cannot see a lease lost
    mid-way through a SINGLE step's own per-job loop -- a collector run
    can process up to `MAX_MESSAGES_PER_SYNC`/many jobs, each potentially
    making a real external research call, so ownership can be confirmed
    lost partway through one step's own execution. Checked here (never
    inside the job-scoring/persistence path itself, which stays durable
    and idempotent either way) so a lease-lost worker stops launching NEW
    external research calls immediately, without needing to wait for the
    whole step to return.
    """
    if is_lease_lost is not None and is_lease_lost():
        return
    if not settings.company_research_auto_enabled or result.recommendation != "APPLY":
        return
    if budget["remaining"] <= 0:
        return
    budget["remaining"] -= 1
    # NEW-002 (Astra R4A): captured as plain scalars BEFORE the call, not
    # read from `record` inside the except block below — see that
    # block's own comment for why.
    job_id = record.id
    company = record.company
    try:
        await CompanyResearchService().get_or_run(db, record, settings)
    except Exception as exc:
        # NEW-002 (Astra R4A): a failure here (e.g. a flush/commit inside
        # CompanyResearchService.get_or_run's own persistence calls) can
        # leave `db` — the SAME shared session the caller uses for core
        # job scoring/persistence — in SQLAlchemy's "pending rollback"
        # state: any FURTHER use of `db` (the next job's
        # score_and_persist, a later commit, even this except block's
        # own `record.id`/`record.company` access) would then raise
        # PendingRollbackError instead of the real, already-logged
        # failure, silently aborting the rest of THIS collector run over
        # what was meant to be a best-effort, isolated failure.
        # `db.rollback()` here is always safe: `record`'s own write
        # (app.db.repositories.upsert_job -> _finalize_job_write) already
        # committed in an EARLIER, separate transaction before this
        # function was ever called — this rollback can only discard
        # whatever uncommitted work `get_or_run`'s own failed attempt
        # left behind in the CURRENT transaction, never that
        # already-durable job write. `job_id`/`company` were captured as
        # plain scalars above (not read from `record` here) so this log
        # line never touches the now-expired ORM object post-rollback.
        db.rollback()
        logger.warning(
            "company_research_auto_run_failed job_id=%s company=%s error_type=%s",
            job_id,
            company,
            type(exc).__name__,
        )


async def run_bundesagentur(
    db: Session,
    settings,
    *,
    touched_jobs: list[TouchedJob] | None = None,
    is_lease_lost: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Fetch + score + persist one Bundesagentur collector run.

    Shared by POST /collectors/bundesagentur/run, the Telegram control
    center's `/run bundesagentur` command, and Stage 8A's automation
    orchestrator (`app.services.automation`) so this logic lives in
    exactly one place. Raises CollectorNotConfiguredError if
    BUNDESAGENTUR_API_KEY isn't set, or a CollectorError subclass if the
    upstream fetch ultimately fails — callers translate these into their
    own presentation (HTTP status code, chat message, or
    AutomationRunStepResult).

    `touched_jobs` (S8C-POOL-001): optional kw-only sink. When provided,
    every job this call successfully scores+persists is appended as a
    `TouchedJob` immediately after that commit — BEFORE the best-effort
    Telegram/auto-research side effects below, so a notification/research
    failure can never remove an already-recorded touch. A job whose
    scoring/persistence itself fails is never appended (see the
    `except Exception` branch's `continue` above the touch point).
    Existing callers (the API endpoint, the Telegram bot command) omit
    this parameter entirely and see no behavior change whatsoever.

    `is_lease_lost` (Codex gate follow-up, Astra R4A lease-loss MEDIUM):
    optional kw-only callback, `None` for every standalone caller (the
    API endpoint, the Telegram bot command) -- only
    `app.services.automation.run_automation_cycle` passes one. Checked
    before each job's auto-research/Telegram-notification calls (never
    before its own scoring/persistence, which stays durable and
    idempotent regardless of lease ownership) so a worker that has
    already confirmed it lost the automation lease stops launching NEW
    external side-effect calls immediately, without waiting for this
    whole run to return. See `_maybe_auto_research`'s own docstring for
    the full rationale.
    """
    if not is_api_key_configured(settings.bundesagentur_api_key):
        raise CollectorNotConfiguredError(
            "Bundesagentur collector is not configured: set BUNDESAGENTUR_API_KEY."
        )

    collector = BundesagenturCollector(
        api_key=settings.bundesagentur_api_key,
        keywords=settings.bundesagentur_search_keywords,
        location=settings.bundesagentur_search_location,
        radius_km=settings.bundesagentur_search_radius_km,
    )

    jobs = await collector.fetch()

    profile = get_or_create_default_profile(db)
    # One notifier per collector run (not per job): send_job() opens its own
    # httpx.AsyncClient per call, so this only avoids repeated construction
    # overhead, but it also keeps the flood-limit pacing below scoped to a
    # single run via one shared notified_count counter.
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_retries=settings.telegram_max_retries,
    )
    created_count = 0
    updated_count = 0
    failed_count = 0
    notified_count = 0
    auto_research_budget = {"remaining": settings.company_research_auto_max_per_run}
    for job in jobs:
        try:
            existing = get_job_by_fingerprint(db, job)
            description = job.description
            if not description.strip() and existing is not None and existing.description.strip():
                # Search responses currently contain no description. A
                # persisted non-empty BA description therefore means detail
                # enrichment already succeeded on an earlier run. Reuse it
                # and re-run the deterministic extractor locally; requiring
                # saved skills too would repeatedly call detail for valid
                # non-technical descriptions where zero matches is expected.
                description = existing.description
                logger.debug(
                    "bundesagentur_detail_reused referenznummer=%s",
                    job.source_reference,
                )
            elif not description.strip() and job.source_reference:
                detail_description = await collector.fetch_detail(job.source_reference)
                if detail_description is not None:
                    description = detail_description
            elif not description.strip():
                logger.warning(
                    "bundesagentur_detail_skipped reason=missing_referenznummer url=%s",
                    job.url,
                )

            extraction = extract_skills(job.title, description)
            all_skills = sorted(
                set(job.skills)
                | set(extraction.must_have_skills)
                | set(extraction.nice_to_have_skills)
            )
            job = job.model_copy(
                update={
                    "description": description,
                    "skills": all_skills,
                    "must_have_skills": extraction.must_have_skills,
                    "nice_to_have_skills": extraction.nice_to_have_skills,
                    "skill_source": extraction.skill_source,
                }
            )
            if existing is not None and description.strip():
                # Stage the BA-only enrichment update in the same transaction
                # committed by upsert_job. If scoring/persistence fails, the
                # surrounding rollback also restores the previous description.
                existing.description = description
            job_record, result, created = score_and_persist(db, profile, job)
        except Exception as exc:
            # A failure scoring/persisting one job (JobScorer bug, DB
            # constraint violation, etc.) must not abort the whole run and
            # lose the jobs already committed before it. db.rollback() is
            # required here: SQLAlchemy leaves the Session unusable after a
            # failed flush/commit until it's rolled back, so without this
            # every job after the first failure would also fail.
            db.rollback()
            failed_count += 1
            logger.warning(
                "bundesagentur_collector_job_persist_failed title=%s company=%s url=%s "
                "error_type=%s",
                job.title,
                job.company,
                job.url,
                type(exc).__name__,
            )
            continue

        if created:
            created_count += 1
        else:
            updated_count += 1

        if touched_jobs is not None:
            touched_jobs.append(
                TouchedJob(
                    job_id=job_record.id,
                    score=job_record.score,
                    status=job_record.status,
                    recommendation=job_record.recommendation,
                )
            )

        await _maybe_auto_research(
            db, settings, job_record, result, auto_research_budget, is_lease_lost=is_lease_lost
        )

        if result.recommendation == "APPLY" and result.score >= settings.min_job_score_to_notify:
            # Notification delivery is best-effort orchestration on top of
            # already-committed persistence: a failed/slow send must not
            # affect created/updated/failed counts or abort the run.
            if notified_count > 0:
                await asyncio.sleep(1)
            # Codex gate follow-up (Astra R4A lease-loss MEDIUM, take 2):
            # rechecked HERE, immediately before send_job() -- covers both
            # "lease lost during the pacing sleep above" (the bug: the
            # previous version checked is_lease_lost() only BEFORE that
            # `await asyncio.sleep(1)`, so a lease lost during the sleep
            # still let send_job() run) and, when no sleep happened at
            # all, the ordinary immediate case. See
            # `_maybe_auto_research`'s own docstring for the full
            # rationale (`is_lease_lost` is None for every standalone
            # caller, so this is always a no-op there).
            if is_lease_lost is None or not is_lease_lost():
                try:
                    sent = await notifier.send_job(job, result)
                except Exception as exc:
                    logger.warning(
                        "bundesagentur_notification_error title=%s company=%s error_type=%s",
                        job.title,
                        job.company,
                        type(exc).__name__,
                    )
                else:
                    if not sent:
                        logger.warning(
                            "bundesagentur_notification_failed title=%s company=%s",
                            job.title,
                            job.company,
                        )
                notified_count += 1

    logger.info(
        "bundesagentur_collector_run fetched=%s created=%s updated=%s skipped_invalid=%s failed=%s",
        len(jobs),
        created_count,
        updated_count,
        collector.skipped_invalid_count,
        failed_count,
    )

    return {
        "fetched": len(jobs),
        "created": created_count,
        "updated": updated_count,
        "skipped_invalid": collector.skipped_invalid_count,
        "failed": failed_count,
    }


async def run_xing(
    db: Session,
    settings,
    *,
    touched_jobs: list[TouchedJob] | None = None,
    is_lease_lost: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Fetch + score + persist one XING mailbox collector run.

    Shared by POST /collectors/xing/run, the Telegram control center's
    `/run xing` command, and Stage 8A's automation orchestrator — see
    `run_bundesagentur` above for the same rationale, including
    `touched_jobs`'s exact semantics (S8C-POOL-001) and `is_lease_lost`'s
    (Codex gate follow-up, Astra R4A lease-loss MEDIUM).
    """
    if not is_configured(settings.xing_mailbox_username) or not is_configured(
        settings.xing_mailbox_app_password
    ):
        raise CollectorNotConfiguredError(
            "XING mailbox collector is not configured: set "
            "XING_MAILBOX_USERNAME and XING_MAILBOX_APP_PASSWORD."
        )

    # Codex gate follow-up (Astra R4A MEDIUM, starvation): the persisted
    # scan watermark from the LAST run -- see
    # app.db.models.XingScanProgressRecord's docstring. `scan_progress`
    # may be None (brand-new installation) or have both fields unset
    # (never successfully advanced yet); either way `XingEmailCollector`
    # treats that identically to "scan everything in the search window",
    # same as before this fix existed.
    # Codex gate follow-up (Astra R4A MEDIUM, mailbox scope): scopes the
    # watermark row to THIS configured mailbox -- see
    # `compute_mailbox_scope`'s own docstring for why `source="xing"`
    # alone is not a safe key (two different mailboxes can coincidentally
    # share a `UIDVALIDITY`).
    mailbox_scope = compute_mailbox_scope(
        settings.xing_mailbox_imap_host,
        settings.xing_mailbox_imap_port,
        settings.xing_mailbox_username,
    )
    scan_progress = get_xing_scan_progress(db, mailbox_scope=mailbox_scope)
    collector = XingEmailCollector(
        imap_host=settings.xing_mailbox_imap_host,
        imap_port=settings.xing_mailbox_imap_port,
        username=settings.xing_mailbox_username,
        app_password=settings.xing_mailbox_app_password,
        lookback_days=settings.xing_lookback_days,
        # Bound to this request's db.Session via closures rather than
        # passed as a constructor `db` param, so the collector itself stays
        # decoupled from SQLAlchemy — see XingEmailCollector's docstring.
        is_message_processed=lambda message_id: is_message_processed(db, "xing", message_id),
        scan_from_uid=scan_progress.confirmed_upto_uid if scan_progress is not None else None,
        expected_uid_validity=scan_progress.uid_validity if scan_progress is not None else None,
    )

    message_batches = await collector.fetch_message_batches()
    jobs = [job for batch in message_batches for job in batch.jobs]

    profile = get_or_create_default_profile(db)
    # One notifier for the whole run (all batches), so the flood-limit pacing
    # via notified_count below is scoped per collector run, not per message.
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_retries=settings.telegram_max_retries,
    )
    created_count = 0
    updated_count = 0
    failed_count = 0
    notified_count = 0
    auto_research_budget = {"remaining": settings.company_research_auto_max_per_run}
    # Codex gate follow-up (Astra R4A MEDIUM, starvation): per-batch
    # persistence outcome, keyed by the batch's own IMAP UID -- combined
    # with `collector.confirmed_uids` below to compute the new
    # contiguous confirmed-handled prefix once every batch has been
    # attempted. Only a batch whose `mark_message_processed` actually ran
    # (i.e. every job in it persisted) counts as "handled" here.
    batch_uid_outcomes: dict[int, bool] = {}
    for batch in message_batches:
        batch_failed = False
        for job in batch.jobs:
            try:
                job_record, result, created = score_and_persist(db, profile, job)
            except Exception as exc:
                # One bad job must not abort the run, but its source message
                # must remain unacknowledged. A later run will parse the whole
                # message again; jobs already committed from this batch are
                # safely deduplicated by fingerprint. Reprocessing is preferred
                # to silently losing the failed job forever.
                db.rollback()
                batch_failed = True
                failed_count += 1
                logger.warning(
                    "xing_collector_job_persist_failed title=%s company=%s url=%s error_type=%s",
                    job.title,
                    job.company,
                    job.url,
                    type(exc).__name__,
                )
                continue

            if created:
                created_count += 1
            else:
                updated_count += 1

            if touched_jobs is not None:
                touched_jobs.append(
                    TouchedJob(
                        job_id=job_record.id,
                        score=job_record.score,
                        status=job_record.status,
                        recommendation=job_record.recommendation,
                    )
                )

            await _maybe_auto_research(
                db,
                settings,
                job_record,
                result,
                auto_research_budget,
                is_lease_lost=is_lease_lost,
            )

            if (
                result.recommendation == "APPLY"
                and result.score >= settings.min_job_score_to_notify
            ):
                # Same best-effort contract as run_bundesagentur: notification
                # failures are orchestration on top of already-committed
                # persistence and must not affect counts or abort the run.
                if notified_count > 0:
                    await asyncio.sleep(1)
                # Codex gate follow-up (Astra R4A lease-loss MEDIUM, take
                # 2): see run_bundesagentur's identical, rechecked-right-
                # before-send_job() guard -- fixes the same "lost during
                # the pacing sleep" gap here too.
                if is_lease_lost is None or not is_lease_lost():
                    try:
                        sent = await notifier.send_job(job, result)
                    except Exception as exc:
                        logger.warning(
                            "xing_notification_error title=%s company=%s error_type=%s",
                            job.title,
                            job.company,
                            type(exc).__name__,
                        )
                    else:
                        if not sent:
                            logger.warning(
                                "xing_notification_failed title=%s company=%s",
                                job.title,
                                job.company,
                            )
                    notified_count += 1

        if not batch_failed:
            mark_message_processed(db, "xing", batch.message_id)
        batch_uid_outcomes[batch.uid] = not batch_failed

    # Codex gate follow-up (Astra R4A HIGH, watermark gap): compute and
    # persist how far the scan can safely resume from next run.
    # `handled_uids` combines every UID this run either (a) confirmed
    # safe to skip without ever yielding a batch (`collector.confirmed_uids`)
    # or (b) yielded a batch that just finished persisting above
    # (`batch_uid_outcomes`, True only if `mark_message_processed` ran).
    #
    # The walk MUST iterate `collector.candidate_uids` -- the full ordered
    # list of UIDs this run considered, INCLUDING ones neither confirmed
    # nor batched (a non-OK FETCH per `_fetch_and_process_message`'s
    # `(None, False)` return, or a UID the session deadline never reached)
    # -- and not merely `sorted(handled_uids)`. A prior version walked
    # `sorted(handled_uids)`: since an unresolved UID is absent from that
    # dict entirely (neither key nor False value), sorting its keys
    # silently DELETED the gap instead of stopping at it, letting a later
    # successfully-handled UID advance the watermark straight past an
    # earlier UID whose FETCH failed -- permanently losing that message
    # (it would never be retried, since the watermark already skips past
    # it). Walking the full ordered candidate list and treating "absent
    # from handled_uids" the same as "present but False" (`.get(uid)` is
    # falsy either way) guarantees the watermark stops at the FIRST
    # unresolved/failed/unreached UID, exactly like a present-and-False
    # entry already did.
    if collector.uid_validity is not None:
        handled_uids: dict[int, bool] = {uid: True for uid in collector.confirmed_uids}
        handled_uids.update(batch_uid_outcomes)
        baseline_uid = (
            scan_progress.confirmed_upto_uid
            if scan_progress is not None and scan_progress.uid_validity == collector.uid_validity
            else None
        )
        new_watermark = baseline_uid
        for uid in collector.candidate_uids:
            if handled_uids.get(uid):
                new_watermark = uid
            else:
                break
        # Codex gate follow-up (Astra R4A MEDIUM take 3, UIDVALIDITY CAS):
        # the epoch THIS run actually observed at the top of the function
        # (before deciding scan_from_uid/computing new_watermark) -- the
        # baseline `advance_xing_scan_progress`'s reset CAS is contingent
        # on, not `collector.uid_validity` (the freshly-observed target).
        observed_uid_validity = scan_progress.uid_validity if scan_progress is not None else None
        advance_xing_scan_progress(
            db,
            uid_validity=collector.uid_validity,
            confirmed_upto_uid=new_watermark,
            mailbox_scope=mailbox_scope,
            observed_uid_validity=observed_uid_validity,
        )

    logger.info(
        "xing_collector_run fetched=%s created=%s updated=%s skipped_invalid=%s failed=%s "
        "deadline_exceeded=%s",
        len(jobs),
        created_count,
        updated_count,
        collector.skipped_invalid_count,
        failed_count,
        collector.deadline_exceeded,
    )

    return {
        "fetched": len(jobs),
        "created": created_count,
        "updated": updated_count,
        "skipped_invalid": collector.skipped_invalid_count,
        "failed": failed_count,
        # NEW-001 (Astra R4A): True if the IMAP session's total deadline
        # fired before every candidate message could be fetched -- see
        # app.collectors.xing_email.XingEmailCollector.deadline_exceeded.
        # _run_step (app.services.automation) treats this as forcing a
        # non-"ok" step status even when every fetched job persisted
        # cleanly, since real work is still pending for this account.
        "deadline_exceeded": collector.deadline_exceeded,
    }
