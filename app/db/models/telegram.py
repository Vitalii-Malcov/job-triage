"""ORM models: telegram (import via `app.db.models`)."""

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import _in_list
from app.models.telegram_operator import TelegramDeliveryState


class TelegramDigestDeliveryRecord(Base):
    """Stage 8E: one persisted delivery attempt of the optional DAILY
    Telegram digest, for one account, for one LOCAL calendar date (in
    `settings.telegram_daily_digest_timezone`) -- the sole idempotency
    primitive that keeps a restart, or more than one standalone
    `python -m app.scheduler` worker process, from sending the same
    day's digest twice. Mirrors `ResponseDraftSendRecord`'s claim/CAS
    shape (see `app.db.response_draft_approval_repository`) applied to
    "send one Telegram message" instead of "send one email".

    **Identity is `(account_key, digest_date)` — `UNIQUE`, enforced by
    the database, not a Python check-then-act read.**
    `app.db.telegram_digest_repository.claim_delivery` always attempts a
    plain INSERT (status=`PENDING`) first and lets a concurrent duplicate
    fail on this constraint -- the same INSERT + IntegrityError-catch
    idiom used throughout this project (e.g.
    `app.db.response_draft_approval_repository.claim_send_attempt`). Only
    the caller that wins the INSERT may attempt the actual Telegram send
    for that date; every other caller (a second worker process, or the
    same worker's next poll tick before the date rolls over) observes the
    existing row and does not resend.

    **`status`.** `PENDING` from the winning claim until the send
    attempt resolves. `SENT` once Telegram's API has POSITIVELY
    confirmed delivery (2xx response) -- terminal, never resent for this
    date. `FAILED` for a send that DEFINITELY did not reach Telegram (a
    connection-level error, or a non-2xx response Telegram itself
    returned) -- the one non-terminal state: a LATER poll tick on the
    SAME still-current date may retry it via `retry_delivery`'s CAS
    (`FAILED -> PENDING`), mirroring
    `app.db.response_draft_approval_repository.retry_send_attempt`.
    `UNCERTAIN` for a send whose outcome could not be proven either way
    (e.g. a network timeout after the request may already have reached
    Telegram) -- terminal, exactly like `ResponseDraftSendRecord.UNCERTAIN`
    (see that model's docstring): retrying an uncertain send risks a
    real duplicate message landing in the operator's chat, so this
    project's conservative default is to never automatically retry it.
    The digest simply resumes normally on the NEXT calendar date.

    **No email content, no job data, no draft/response text lives
    here** -- only `account_key` (an identity, not a secret -- mirrors
    every other `account_key` column in this project), `digest_date`,
    delivery bookkeeping, and a truncated, sanitized `last_error` (never
    a raw Telegram response body or bot token).
    """

    __tablename__ = "telegram_digest_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "account_key", "digest_date", name="uq_telegram_digest_deliveries_account_date"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_telegram_digest_deliveries_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


# Stage 9A: every state a `TelegramVacancyReviewRecord` row can be in --
# kept as one module-level tuple so the ORM CheckConstraint, the Alembic
# migration, and the repository's own transitions can never drift apart.
VACANCY_REVIEW_STATES = (
    "DISCOVERED",
    "QUEUED_FOR_REVIEW",
    "SENDING",
    "TELEGRAM_SENT",
    "DELIVERY_UNCERTAIN",
    "DELIVERY_FAILED",
    "SAVED",
    "SKIPPED",
)


class TelegramVacancyReviewRecord(Base):
    """Stage 9A: the Telegram vacancy-feed review state of ONE `JobRecord`.

    **Deliberately separate from `JobRecord.status`.** `JobRecord.status`
    is the APPLICATION lifecycle (`ApplicationStatus`: NEW/SAVED/APPLIED/
    ...). Whether a vacancy card was delivered to Telegram, and whether
    the operator pressed Save/Skip on it, is REVIEW state -- mixing the two
    would make e.g. "Telegram send failed" indistinguishable from an
    application-lifecycle transition. Nothing in Stage 9A writes
    `JobRecord.status`.

    **Identity is `job_id` -- `UNIQUE`, enforced by the database.** A
    "logical vacancy" is already exactly one `JobRecord` (fingerprint
    dedup, `app.db.repositories.upsert_job`), so one row per job_id is
    what guarantees the same vacancy is never carded twice: every
    re-collection of the same fingerprint lands on the SAME row, and
    `app.db.telegram_vacancy_review_repository.ensure_review` is an
    INSERT + IntegrityError-catch, never a check-then-act.

    **`state`:**
    - `DISCOVERED` -- collected and persisted, not (yet) eligible for review.
    - `QUEUED_FOR_REVIEW` -- eligible, waiting for delivery. Also where a
      provably-failed send (`TelegramSendOutcome.FAILED`) returns to, so the
      next sweep retries it.
    - `SENDING` -- claimed by exactly one sweep (CAS), send in flight. A
      claim older than the stale TTL is reconciled to `DELIVERY_UNCERTAIN`.
    - `TELEGRAM_SENT` -- Telegram positively confirmed delivery.
    - `DELIVERY_UNCERTAIN` -- outcome could not be proven either way;
      terminal for automatic delivery (retrying risks a duplicate card,
      same policy as `TelegramDigestDeliveryRecord.UNCERTAIN`).
    - `DELIVERY_FAILED` -- provably failed `attempt_count` times in a row
      (bounded so a permanently rejected message can't retry forever).
    - `SAVED` / `SKIPPED` -- the operator's decision via the card buttons.

    `callback_token` is a random, opaque, per-row token embedded in the
    card's inline-button `callback_data` -- callbacks are resolved ONLY by
    this token, never by a job id the client sends back.

    No job content lives here -- only bookkeeping and a short, sanitized
    `last_error` (an exception type / outcome name, never a Telegram
    response body or the bot token).
    """

    __tablename__ = "telegram_vacancy_reviews"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_telegram_vacancy_reviews_job_id"),
        UniqueConstraint("callback_token", name="uq_telegram_vacancy_reviews_callback_token"),
        CheckConstraint(
            "state IN ("
            "'DISCOVERED', 'QUEUED_FOR_REVIEW', 'SENDING', 'TELEGRAM_SENT', "
            "'DELIVERY_UNCERTAIN', 'DELIVERY_FAILED', 'SAVED', 'SKIPPED')",
            name="ck_telegram_vacancy_reviews_state_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    callback_token: Mapped[str] = mapped_column(String(32), nullable=False)
    # Identity of the CURRENT `SENDING` claim (random, set by the winning
    # claim, cleared whenever the row leaves `SENDING`). Every `SENDING -> *`
    # transition must present it, so a stale worker can never resolve or
    # release a newer claim. NULL outside `SENDING`.
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class TelegramBewerbungPreparationRecord(Base):
    """Stage 9B: the workflow ledger for ONE vacancy-review row's Telegram
    "Bewerbung erstellen" request -- who currently owns the preparation,
    which exact immutable 6B/6C/6D package was published, and whether its
    preview reached the operator's private chat.

    **Workflow metadata only.** No CV/match/letter content is copied here:
    `match_id`/`cv_draft_id`/`bewerbung_draft_id` pin immutable snapshot
    rows (deliberately not FKs, same retention rationale as
    `BewerbungDraftRecord` -- every reader validates they still exist and
    belong together, failing closed on a dangling pin). The only stored
    text is the frozen summary `preview_text`, so a re-sent preview always
    shows exactly what was prepared, never facts re-derived from a later
    profile. `review_id` IS an FK (ON DELETE CASCADE): the ledger is
    operational state of that review row, nothing else.

    **Preparation state** (`state`): `PREPARING` (one owner, identified by
    `generation` + `prep_claim_token`), `PREPARED` (a consistent package is
    published), `FAILED` (sanitized `last_error` code). Stale ownership is a
    recovery TRANSITION (`PREPARING -> FAILED`), never a fourth state. Every
    `PREPARING -> *` UPDATE matches `id`, `state`, `generation` and
    `prep_claim_token` in SQL, so a stale worker can never publish, fail, or
    attach a letter to a newer generation. `input_identity` is a digest of
    the inputs the package was prepared from; `attempt_count` counts
    genuine attempts for the current `input_identity` (max 5).

    **Preview delivery** (`preview_state`): `NONE -> SENDING -> SENT /
    FAILED / UNCERTAIN`, fenced exactly like Stage 9A card delivery --
    `SENDING` (with `preview_claim_token`) is committed BEFORE the Telegram
    request, and only that token + `generation` can resolve it. A stale
    `SENDING` becomes `UNCERTAIN` (an external side effect may have
    happened), and `UNCERTAIN` is never retried automatically.

    `package_token` is the opaque capability embedded in the preview's
    "Vorschau" buttons; it is replaced on every publication, so an old
    button can never display a newer package.
    """

    __tablename__ = "telegram_bewerbung_preparations"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_telegram_bewerbung_preparations_review_id"),
        UniqueConstraint("package_token", name="uq_telegram_bewerbung_preparations_package_token"),
        CheckConstraint(
            "state IN ('PREPARING', 'PREPARED', 'FAILED')",
            name="ck_telegram_bewerbung_preparations_state_valid",
        ),
        CheckConstraint(
            "generation >= 1", name="ck_telegram_bewerbung_preparations_generation_positive"
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_telegram_bewerbung_preparations_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "(state = 'PREPARING' AND prep_claim_token IS NOT NULL "
            "AND prep_claim_started_at IS NOT NULL) "
            "OR (state <> 'PREPARING' AND prep_claim_token IS NULL)",
            name="ck_telegram_bewerbung_preparations_prep_token_state",
        ),
        CheckConstraint(
            "state <> 'PREPARED' OR (match_id IS NOT NULL AND cv_draft_id IS NOT NULL "
            "AND bewerbung_draft_id IS NOT NULL AND package_token IS NOT NULL "
            "AND preview_text IS NOT NULL AND preview_renderer_version IS NOT NULL)",
            name="ck_telegram_bewerbung_preparations_prepared_artifacts",
        ),
        CheckConstraint(
            "preview_state IN ('NONE', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_telegram_bewerbung_preparations_preview_state_valid",
        ),
        CheckConstraint(
            "(preview_state = 'SENDING' AND preview_claim_token IS NOT NULL "
            "AND preview_claim_started_at IS NOT NULL) "
            "OR (preview_state <> 'SENDING' AND preview_claim_token IS NULL)",
            name="ck_telegram_bewerbung_preparations_preview_token_state",
        ),
        CheckConstraint(
            "preview_state <> 'SENDING' OR state = 'PREPARED'",
            name="ck_telegram_bewerbung_preparations_preview_requires_prepared",
        ),
        Index(
            "ix_telegram_bewerbung_preparations_state_claim",
            "state",
            "prep_claim_started_at",
        ),
        Index(
            "ix_telegram_bewerbung_preparations_preview_claim",
            "preview_state",
            "preview_claim_started_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("telegram_vacancy_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    prep_claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prep_claim_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cv_draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bewerbung_draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preview_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_renderer_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)

    preview_state: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    preview_claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preview_claim_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    preview_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    preview_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class TelegramBewerbungApprovalRecord(Base):
    """Stage 9C: the immutable, historical link between ONE Stage 9B
    package generation and the ONE Stage 6E review created for it from
    Telegram, plus the opaque decision capability that Telegram buttons
    carry (see app/services/telegram_bewerbung_approval.py).

    **Linkage only -- never a second approval state machine.** Review
    status, decision time, the approved revision and the match/CV/letter
    pins all live in Stage 6E (`ApplicationPackageReviewRecord`); nothing
    here duplicates them. A row is inserted once, in the same transaction as
    the review and its revision 1, and is never updated (so there is no
    `updated_at`).

    **Exact binding.** `(preparation_id, generation, package_token,
    input_identity)` snapshot the exact Stage 9B package the review was
    created for; `(review_id, bound_review_version)` pins the exact
    immutable revision Telegram displays and decides (unique through 6E's
    UNIQUE(review_id, revision_number)). Stage 9C v1 binds only revision 1;
    an API PATCH makes the capability permanently non-decidable.

    **Historical retention.** `preparation_id` and `review_id` are
    deliberately NOT foreign keys (and never cascade): deleting the
    operational Stage 9B ledger row (or the Stage 9A review it cascades
    from) must not erase approval lineage that Stage 6E keeps as permanent
    audit data. Every reader validates the referenced rows and fails closed
    when a current preparation is required but missing.

    UNIQUE(preparation_id, generation) is the database arbiter of "at most
    one Telegram-linked review per generation"; `approval_capability` is a
    96-bit bearer credential (16 base64url characters).
    """

    __tablename__ = "telegram_bewerbung_approvals"
    __table_args__ = (
        UniqueConstraint(
            "preparation_id",
            "generation",
            name="uq_telegram_bewerbung_approvals_preparation_generation",
        ),
        UniqueConstraint("review_id", name="uq_telegram_bewerbung_approvals_review_id"),
        UniqueConstraint(
            "approval_capability", name="uq_telegram_bewerbung_approvals_approval_capability"
        ),
        CheckConstraint(
            "generation >= 1", name="ck_telegram_bewerbung_approvals_generation_positive"
        ),
        CheckConstraint(
            "bound_review_version = 1",
            name="ck_telegram_bewerbung_approvals_bound_review_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preparation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    package_token: Mapped[str] = mapped_column(String(32), nullable=False)
    input_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    review_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bound_review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_capability: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


TELEGRAM_DELIVERY_STATE_VALUES: tuple[str, ...] = tuple(s.value for s in TelegramDeliveryState)


OPERATOR_CARD_CHECKS: dict[str, str] = {
    "ck_telegram_operator_cards_state_valid": _in_list(
        "delivery_state", TELEGRAM_DELIVERY_STATE_VALUES
    ),
    "ck_telegram_operator_cards_attempts_nonnegative": "attempt_count >= 0",
}


LIFECYCLE_NOTIFICATION_CHECKS: dict[str, str] = {
    "ck_telegram_lifecycle_notifications_state_valid": _in_list(
        "delivery_state", TELEGRAM_DELIVERY_STATE_VALUES
    ),
    "ck_telegram_lifecycle_notifications_attempts_nonnegative": "attempt_count >= 0",
}


class TelegramOperatorCardRecord(Base):
    """Stage 16: one operator card for one review target -- an application
    plus the exact review package it was rendered for.

    `target_key` (`app:<id>:pkg:<id|none>`) is UNIQUE: a target gets at most
    one card, and a provably failed send is retried on the SAME row, so the
    operator is never sent duplicate cards. `callback_token` is the opaque
    random capability carried in the buttons' callback_data; a callback is
    only ever resolved through it, and the target is always re-read from
    the database before anything is decided.
    """

    __tablename__ = "telegram_operator_cards"
    __table_args__ = (
        UniqueConstraint("target_key", name="uq_telegram_operator_cards_target_key"),
        UniqueConstraint("callback_token", name="uq_telegram_operator_cards_callback_token"),
        *(CheckConstraint(body, name=name) for name, body in OPERATOR_CARD_CHECKS.items()),
        Index("ix_telegram_operator_cards_application_id", "application_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    review_package_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_key: Mapped[str] = mapped_column(String(80), nullable=False)
    callback_token: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(20), nullable=False)
    approve_offered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    card_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramLifecycleNotificationRecord(Base):
    """Stage 16 delivery ledger: at most one Telegram notification per
    Stage 15 lifecycle event (`UNIQUE(application_event_id)`).

    Processing the same event again finds the row and sends nothing. A
    provably FAILED send may be retried (bounded) on the same row; an
    UNCERTAIN one never is. Notification delivery is bookkeeping only: it
    never changes an application or its events.
    """

    __tablename__ = "telegram_lifecycle_notifications"
    __table_args__ = (
        UniqueConstraint(
            "application_event_id", name="uq_telegram_lifecycle_notifications_event_id"
        ),
        *(CheckConstraint(body, name=name) for name, body in LIFECYCLE_NOTIFICATION_CHECKS.items()),
        Index("ix_telegram_lifecycle_notifications_application_id", "application_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("application_events.id", ondelete="RESTRICT"), nullable=False
    )
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_state: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
