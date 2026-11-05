"""ORM models: automation (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AutomationRunRecord(Base):
    """Stage 8A: one persisted, end-to-end orchestrated job-search cycle
    for one account — the audit trail of WHEN an automation run happened,
    WHICH existing steps (collectors) it coordinated, and what each one's
    outcome was. This table owns none of the actual collection/scoring
    logic — `app.services.automation.run_automation_cycle` orchestrates
    the SAME `app.api.routes._run_bundesagentur`/`_run_xing` helpers the
    individual `/collectors/*/run` endpoints and the Telegram control
    center already call, so this is coordination bookkeeping only, never
    a second implementation of collection/dedup/scoring.

    **`account_key` (mirrors GMAIL-002's convention elsewhere).** The
    normalized `GMAIL_USERNAME` this run was scoped to — every read is
    filtered by it, so a later account change can never leak or mix a
    previous account's run history, exactly like Gmail/follow-up records.

    **`status`** is one of `RUNNING` / `COMPLETED` / `PARTIAL` / `FAILED`:
    `RUNNING` from creation until the orchestrator finishes; `COMPLETED`
    if every coordinated step succeeded; `PARTIAL` if at least one
    succeeded and at least one did not; `FAILED` if none did. Terminal
    states are never left ambiguous — the caller always ends the run in
    exactly one of these three.

    **`results_json`** is a JSON object keyed by step name (e.g.
    `"bundesagentur"`, `"xing"`), each value shaped like
    `{"status": "ok"|"not_configured"|"failed", "counters": {...} |
    null, "error_type": str | null}` — `counters` is the step's own
    already-existing return shape (e.g. `{"fetched":.., "created":..,
    ...}`), never re-derived or duplicated here. `error_summary` is a
    short, human-readable, SANITIZED string built only from step names
    and `type(exc).__name__` — mirrors this project's GMAIL-003
    convention (app/providers/email/base.py's `GmailProviderError`
    docstring) of never persisting/returning raw upstream exception text,
    which could otherwise carry back a server-echoed detail.

    **Concurrency (fail-closed, not serialized).** `uq_automation_runs_one_running_per_account`
    is a PARTIAL unique index — `UNIQUE(account_key) WHERE status =
    'RUNNING'` — the sole arbiter of "at most one RUNNING run per
    account at a time", enforced by the database, not a Python
    check-then-act read. S8A-001 (Codex re-review): declared with BOTH
    `sqlite_where` and `postgresql_where` (identical predicate) so this
    project's SQLite deployment and a future PostgreSQL one describe the
    exact same semantics — see
    tests/test_automation_run_index_dialects.py for the dialect-level
    proof (both compile the same `WHERE status = 'RUNNING'` clause).
    `app.db.automation_repository.create_running_run` always attempts a
    plain INSERT first and lets a concurrent duplicate fail on this
    constraint (caught and translated into
    `AutomationRunAlreadyInProgressError`, mapped to 409) — the same
    INSERT + IntegrityError-catch idiom used throughout this project
    (e.g. `app.db.follow_up_approval_repository.claim_send_attempt`).

    **Crash recovery via an ownership-aware lease (S8A-002, Codex
    re-review).** `lease_holder`/`lease_expires_at` mirror Stage 7E's
    per-Gmail-thread lock (`GmailThreadRecord.lock_holder`/
    `lock_expires_at`, see that model's docstring) — a generic,
    time-bounded "who currently owns this RUNNING row, until when"
    primitive. `app.db.automation_repository.create_running_run` claims
    both atomically on INSERT; `app.services.automation`'s
    `_RunLeaseHeartbeat` renews `lease_expires_at` periodically for as
    long as `run_automation_cycle` is executing, via the DEDICATED
    `renew_run_lease` CAS (never `create_running_run`'s own claim path —
    a renewal must fail the instant the lease has expired, even if
    nobody else has taken it over yet; it must never silently resume as
    though ownership had been continuous — see `renew_run_lease`'s
    docstring). A process that crashes (or whose heartbeat otherwise
    stops) leaves the lease to expire on its own schedule rather than
    blocking that account's automation forever: the NEXT request to
    start a run finds a stale RUNNING row (`lease_expires_at` in the
    past) and atomically reconciles it to `FAILED` before claiming a
    fresh run — a LIVE lease (not yet expired) still fails closed
    (`AutomationRunAlreadyInProgressError`, 409), exactly like before;
    only a genuinely expired one is ever reclaimed, and never silently —
    the reconciliation itself is a CAS, so at most one concurrent
    requester ever wins it.
    """

    __tablename__ = "automation_runs"
    __table_args__ = (
        Index(
            "uq_automation_runs_one_running_per_account",
            "account_key",
            unique=True,
            sqlite_where=text("status = 'RUNNING'"),
            postgresql_where=text("status = 'RUNNING'"),
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_automation_runs_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    results_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # S8A-002: ownership-aware lease — see class docstring's "Crash
    # recovery via an ownership-aware lease" section.
    lease_holder: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AutomationScheduleRecord(Base):
    """Stage 8B: one persisted row per account tracking when its next
    scheduled `app.services.automation.run_automation_cycle` cycle is
    due. This table owns ONLY schedule-arbitration state -- it never
    duplicates anything already recorded by `AutomationRunRecord`
    (status/results/lease); `last_run_id` is a plain informational
    pointer to the most recent triggered run, not a foreign key this
    table depends on for correctness.

    **One row per account.** `account_key` is DB-enforced UNIQUE
    (`uq_automation_schedules_account_key`) -- the same normalized
    identity `AutomationRunRecord.account_key` already uses (mirrors
    GMAIL-002's convention elsewhere in this project).

    **Multi-process claim (fail-closed, at-most-once).** `next_run_at` is
    the sole arbiter of "is a slot due right now, and has it already been
    claimed". `app.db.automation_schedule_repository.claim_due_schedule`
    performs a single atomic `UPDATE ... WHERE account_key = :account_key
    AND next_run_at = :observed_next_run_at AND next_run_at <= :now` --
    exactly one concurrent claimer's UPDATE can ever match a given
    `next_run_at` value (every winning claim immediately moves
    `next_run_at` into the future as part of the SAME statement), so two
    scheduler processes racing the same due slot can never both trigger a
    cycle. This is a plain conditional UPDATE (portable SQL), not a
    SELECT ... FOR UPDATE row lock or any SQLite-only construct -- it
    compiles and behaves identically on SQLite and PostgreSQL (see
    tests/test_automation_schedule_migration.py).

    **Coalescing, not catch-up.** A winning claim advances `next_run_at`
    to `now + interval_seconds` (relative to the claim moment), never to
    `previous_next_run_at + interval_seconds` repeated N times -- so a
    worker that was offline for many missed intervals runs exactly ONE
    cycle on return, then resumes its normal cadence. There is
    deliberately no backlog/replay queue.

    **At-most-once, not exactly-once (accepted tradeoff).** `next_run_at`
    is advanced as part of the SAME claim UPDATE that "reserves" the
    slot, BEFORE `run_automation_cycle` is ever called. If the worker
    process crashes after a successful claim but before (or during) that
    call, this single slot is simply skipped -- there is no separate
    heartbeat/lease/retry subsystem for schedule claims in Stage 8B (the
    existing `AutomationRunRecord.lease_holder`/`lease_expires_at` from
    Stage 8A already protects the ACTUAL run's ownership once it starts;
    stacking a second lease system on top of the schedule slot itself
    would be redundant machinery for a failure mode already bounded by
    "wait for the next normal interval").

    **No first-run backlog.** `app.db.automation_schedule_repository.get_or_create_schedule`
    seeds a brand-new row with `next_run_at = now` (immediately due) --
    the deterministic "first scheduler start creates a due schedule, can
    run immediately" semantics this stage documents, not an arbitrary
    delay before the very first cycle.
    """

    __tablename__ = "automation_schedules"
    __table_args__ = (UniqueConstraint("account_key", name="uq_automation_schedules_account_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("automation_runs.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AutomationMailProgressRecord(Base):
    """Stage 8D: one persisted row per account tracking how far the
    Gmail response-draft cycle and the follow-up proposal cycle have
    each progressed -- crash-safe resumption state for
    `app.services.automation_gmail`/`app.services.automation_follow_up`,
    analogous to `AutomationScheduleRecord`'s "one row per account"
    shape but for IN-RUN processing cursors rather than run-scheduling.

    **Why a persisted cursor, not an in-memory "messages touched this
    run" list.** `app.providers.email.imap.GmailImapProvider` (Stage 7A)
    deliberately skips already-persisted UIDs on every sync (see
    `get_known_uids`) -- so if a process crashes AFTER a Gmail message is
    persisted but BEFORE it is analyzed/drafted, an in-memory-only
    "touched this run" design would lose that message forever: no later
    sync would ever re-surface it. `gmail_after_message_id` instead
    anchors progress to `GmailMessageRecord.id` itself (already-persisted,
    monotonic, account-scoped), so a crash mid-cycle only ever costs
    re-attempting the SAME message range on the next run -- never a
    silently skipped one.

    **Two independent cursors, two independent semantics.** Gmail message
    processing is a one-directional catch-up scan (oldest-first, never
    reset -- see `gmail_after_message_id`'s own column comment) — new
    messages simply get larger ids and are naturally reached later. Follow
    -up scanning is a bounded ROUND-ROBIN over currently-`APPLIED` jobs
    (see `follow_up_after_job_id`'s own column comment) -- it must
    eventually wrap back to the oldest APPLIED job so a job that becomes
    newly due (purely because time passed) is periodically re-checked,
    which a one-directional cursor could never achieve on its own.

    **`account_key` (mirrors GMAIL-002's convention elsewhere).** DB
    -enforced UNIQUE (`uq_automation_mail_progress_account_key`) -- one
    row per account, same normalized identity
    `AutomationRunRecord.account_key` already uses.

    **Concurrency (CAS, not blind writes) — S8D-PROGRESS.**
    `app.db.automation_mail_progress_repository.advance_gmail_cursor`/
    `advance_follow_up_cursor` perform a single atomic `UPDATE ... WHERE
    account_key = :account_key AND <cursor column> = :expected_cursor`
    -- exactly like `claim_due_schedule`'s CAS. `AutomationRunRecord`'s
    own lease/heartbeat already prevents ordinary same-account
    concurrency, but a run that briefly continues after losing its lease
    (heartbeat renewal races a replacement run's claim) must never be
    able to silently clobber a NEWER owner's already-advanced progress --
    the CAS's `expected_cursor` mismatch makes that fail closed instead,
    exactly mirroring the run-level lease's own fail-closed contract.

    No email content lives here -- only technical integer cursors.
    """

    __tablename__ = "automation_mail_progress"
    __table_args__ = (
        UniqueConstraint("account_key", name="uq_automation_mail_progress_account_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False)

    # AUD-004 (Astra R3): NO LONGER READ OR WRITTEN by
    # app.services.automation_gmail.prepare_gmail_response_drafts -- an
    # `id`-ordered watermark is unsafe under PostgreSQL's
    # commit-visibility semantics (a lower-`id` row can commit after a
    # higher-`id` one, permanently skipping it under `id > watermark`).
    # Message completion tracking moved to a durable PER-MESSAGE marker,
    # `GmailMessageRecord.automation_processed_at` (see that column's own
    # docstring). This column/its CAS primitive (`advance_gmail_cursor`)
    # are kept, unchanged and still tested, purely as a still-valid
    # generic building block -- dropping either would be a destructive
    # migration for zero benefit; nothing in this project's runtime path
    # relies on this column's value anymore.
    gmail_after_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The largest JobRecord.id (status=APPLIED) fully evaluated for
    # follow-up eligibility so far, in the CURRENT round-robin pass, for
    # this account. NULL means "start the pass from the oldest currently
    # -APPLIED job". UNLIKE gmail_after_message_id, this IS periodically
    # reset back to NULL (via the same CAS primitive) once a full pass
    # reaches the end of currently-APPLIED jobs -- see
    # app.services.automation_follow_up's module docstring for the
    # wrap-around rationale (a job's follow-up eligibility can become due
    # purely because time passed, with no new Gmail activity to "wake" it,
    # so periodic re-scanning from the top is required).
    follow_up_after_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
