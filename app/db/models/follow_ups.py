"""ORM models: follow ups (import via `app.db.models`)."""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
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
from app.db.models.response_drafts import LIVE_SEND_STATUS_PREDICATE


class FollowUpProposalRecord(Base):
    """An immutable, auditable Stage 7E follow-up PROPOSAL for one tracked
    `JobRecord` — see app/services/follow_up.py for the eligibility engine
    and app/agents/follow_up_generator.py for the deterministic,
    template-based content generation.

    **INFORMATION ONLY — a stored suggestion, never an action.** Creating
    a row here never sends email, never mutates `JobRecord.status`, and
    never mutates mailbox state — same hard boundary as
    `ResponseDraftRecord` (see that model's docstring), applied to a
    candidate-initiated follow-up instead of a reply. `requires_human_review`
    is always `True` for every row this stage writes — approval/send are
    owned by `FollowUpApprovalRecord`/`FollowUpSendRecord` below, exactly
    mirroring the Stage 7C/7D split.

    **`anchor_gmail_message_id` is the correspondence anchor** — the
    latest real `OUTBOUND` `GmailMessageRecord` in the job's matched
    thread this follow-up is chasing (see
    app.services.follow_up_eligibility's eligibility rule for exactly how
    it is chosen — never `JobRecord.first_seen_at`/`last_seen_at`, per
    CLAUDE.md). `UNIQUE(account_key, anchor_gmail_message_id)` is the
    dedup identity: at most one follow-up proposal can ever exist per
    correspondence anchor, so re-running the eligibility scan is
    idempotent (a second evaluation of the same still-due anchor returns
    the existing row, never a duplicate) — mirrors
    `ResponseDraftRecord`'s own idempotent-revision convention, except a
    follow-up has exactly one anchor-scoped identity rather than a
    version-bumped one, since nothing about a follow-up's own inputs is
    expected to change between evaluations the way a re-analyzed message
    or edited candidate profile can for Stage 7C.

    **`job_id`/`gmail_thread_id` are deliberately not ForeignKeys** — same
    "traceability, not identity" rationale as
    `GmailMessageAnalysisRecord.matched_job_id`'s own docstring.
    `anchor_gmail_message_id` IS a real ForeignKey (`ondelete="CASCADE"`)
    — a follow-up proposal has no meaning independent of the message it
    anchors to.

    **Never invents facts.** Generated `subject`/`body` are built
    exclusively from already-trusted stored facts (candidate name only if
    provenance-confirmed, job title/company only from a trusted
    `JobRecord.source` — see app.services.follow_up.TRUSTED job-source
    reuse of app.services.response_draft.TRUSTED_JOB_SOURCES) — never from
    email content itself, which is only ever used to pick a DE/EN
    template set (mirrors app.agents.response_draft_generator's own
    trust boundary).
    """

    __tablename__ = "follow_up_proposals"
    __table_args__ = (
        # identity now includes
        # `input_fingerprint`, not just the anchor — see that column's
        # docstring below. A still-matching re-evaluation of the same
        # anchor with UNCHANGED trusted inputs is idempotent exactly as
        # before; CHANGED inputs (job facts, candidate profile revision,
        # language, generator/config version) now produce a NEW proposal
        # revision instead of silently reusing a stale one.
        UniqueConstraint(
            "account_key",
            "anchor_gmail_message_id",
            "input_fingerprint",
            name="uq_follow_up_proposals_anchor_fingerprint",
        ),
        CheckConstraint("language IN ('de', 'en')", name="ck_follow_up_proposals_language_valid"),
        CheckConstraint("status IN ('PROPOSED')", name="ck_follow_up_proposals_status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    # Not a ForeignKey — see class docstring.
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Denormalized traceability copy (derivable from anchor_gmail_message_id
    # via gmail_messages.thread_id) — kept for cheap querying without a
    # join, not part of this row's identity. Not a ForeignKey — see class
    # docstring.
    gmail_thread_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    anchor_gmail_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    eligibility_reason: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # the single canonical external recipient
    # this follow-up would be sent to, derived and VALIDATED (non-empty,
    # unambiguous, not self, well-formed, no CRLF) from the anchor
    # OUTBOUND message's own `to_addresses` at proposal-build time — see
    # app.services.follow_up_recipient.derive_canonical_recipient. Never
    # re-derived from the anchor at send time; see
    # FollowUpApprovalRecord.pinned_recipient for why the approval pins a
    # verbatim copy instead of a live re-read.
    recipient: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")

    # SHA-256 hex digest over every trusted
    # input this proposal's content depends on (job_id, thread_id, anchor,
    # trusted job title/company, candidate name/profile_version, recipient,
    # language, provider, generator_version) — see
    # app.services.follow_up.compute_follow_up_input_fingerprint. Part of
    # this row's UNIQUE identity (see __table_args__ above): re-evaluating
    # the same anchor after any of these inputs changed produces a NEW row
    # rather than returning a stale one, and an approval recorded against
    # an old fingerprint can never be reinterpreted as authorizing new
    # content (approvals key off `follow_up_proposal_id`, which changes
    # too).
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROPOSED")
    # Always True for every row this stage writes — see class docstring.
    # Not DB-CHECK-enforced (mirrors ResponseDraftRecord.requires_human_review's
    # own convention) — enforced by construction in app/services/follow_up.py.
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FollowUpApprovalRecord(Base):
    """An immutable human APPROVE/REJECT decision on one exact
    `FollowUpProposalRecord` (Stage 7E) — mirrors
    `ResponseDraftApprovalRecord` exactly (see that model's docstring for
    the full "pins the exact content" / "one decision per revision, ever"
    rationale, both of which apply here unchanged). `HUMAN APPROVAL = NO
    FOLLOW-UP SEND` is enforced the same way: see
    app/services/follow_up_send.py.
    """

    __tablename__ = "follow_up_approvals"
    __table_args__ = (
        UniqueConstraint("follow_up_proposal_id", name="uq_follow_up_approvals_follow_up_proposal"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="ck_follow_up_approvals_decision_valid"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    follow_up_proposal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only (the proposal's own anchor_gmail_message_id)
    # — same convention as ResponseDraftApprovalRecord.gmail_message_id.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    pinned_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    pinned_body: Mapped[str] = mapped_column(Text, nullable=False)
    # verbatim copy of the approved proposal's
    # own validated `recipient` at decision time — same "pin what the human
    # saw" rationale as pinned_subject/pinned_body above, and the exact
    # value app.services.follow_up_send sends to (never re-derived live at
    # send time) — see that module's module docstring.
    pinned_recipient: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FollowUpSendRecord(Base):
    """The DB-enforced atomic arbiter of "has this approved follow-up
    already been sent, or is a send currently in flight" (Stage 7E) —
    mirrors `ResponseDraftSendRecord` exactly, including the CAS-guarded
    PENDING/SENT/FAILED/UNCERTAIN state machine and the fail-closed,
    never-auto-retried `UNCERTAIN` terminal state for an ambiguous SMTP
    outcome — see that model's docstring for the full rationale, which
    applies here unchanged. See app/services/follow_up_send.py for the
    orchestration that drives these transitions.

    **At most one live follow-up per correspondence anchor, across ALL
    proposal revisions (Stage 12).** `gmail_message_id` here is the
    proposal's `anchor_gmail_message_id` (the thread's latest OUTBOUND
    message). Proposals are unique per `(anchor, input_fingerprint)`, so a
    fingerprint change (candidate-profile edit, job title edit, generator
    version) yields a second proposal for the SAME anchor; send-time
    revalidation checks the anchor is still current, which it still is
    until the first follow-up has been synced back from the Sent mailbox.
    `uq_follow_up_sends_live_per_anchor` (`UNIQUE(account_key,
    gmail_message_id) WHERE status IN ('PENDING', 'SENT', 'UNCERTAIN')`)
    closes that window at the database level -- the same predicate and
    rationale as `ResponseDraftSendRecord`'s per-message index.
    """

    __tablename__ = "follow_up_sends"
    __table_args__ = (
        UniqueConstraint("follow_up_proposal_id", name="uq_follow_up_sends_follow_up_proposal"),
        Index(
            "uq_follow_up_sends_live_per_anchor",
            "account_key",
            "gmail_message_id",
            unique=True,
            sqlite_where=text(LIVE_SEND_STATUS_PREDICATE),
            postgresql_where=text(LIVE_SEND_STATUS_PREDICATE),
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_follow_up_sends_status_valid",
        ),
        CheckConstraint("attempt_count > 0", name="ck_follow_up_sends_attempt_count_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    follow_up_proposal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_up_approvals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only — see FollowUpApprovalRecord's own
    # convention.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # False for the entire
    # window between winning the PENDING claim and the instant right
    # before `OutboundEmailProvider.send` is actually invoked; flipped to
    # True by a dedicated CAS (`app.db.follow_up_approval_repository
    # .begin_transmission`) immediately before that call, in the same
    # request that will make it. A row found PENDING with
    # `send_attempted=False` is PROVABLY pre-transmission — the process
    # that claimed it crashed (or never got that far) before any network
    # call was made, so it is always safe to let a later request take over
    # (see `app.services.follow_up_send._resolve_existing_send_record`).
    # A row found PENDING with `send_attempted=True` means transmission may
    # already be underway (either a live concurrent request, or a crash
    # mid-send) — indistinguishable from here, so it is NEVER retried;
    # instead it is moved to the fail-closed terminal `UNCERTAIN` state.
    # `begin_transmission`'s own CAS (`WHERE send_attempted=False`) is what
    # makes "who gets to actually call the provider" mutually exclusive
    # between concurrent requests — this column is the single source of
    # truth for that exclusivity, not `status` alone.
    send_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
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
