"""ORM models: response drafts (import via `app.db.models`)."""

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


class ResponseDraftRecord(Base):
    """An immutable, versioned Stage 7C response-draft PROPOSAL for one
    Stage 7B `GmailMessageAnalysisRecord` — see
    app/services/response_draft.py for orchestration and
    app/agents/response_draft_generator.py for the deterministic,
    template-based, no-LLM content generation itself.

    **INFORMATION ONLY — a stored suggestion, never an action.** Nothing
    that reads this table sends email, creates a Gmail draft, replies,
    forwards, or mutates mailbox/`JobRecord`/`ApplicationStatus` state —
    same hard boundary as `GmailMessageAnalysisRecord` (see that class's
    docstring), one stage further downstream. `requires_human_review` is
    always `True` for every row Stage 7C writes (spec-mandated; Stage 7D,
    not this stage, owns approval/send). `status='PROPOSED'` never means
    "ready to send" — only that a template produced draft text a human
    can review, edit, and send manually.

    **`status`.** `'PROPOSED'` (subject/body populated) for the bounded
    set of classifications a reply plausibly makes sense for (see
    `app.agents.response_draft_generator.SUPPORTED_RESPONSE_CLASSIFICATIONS`).
    `'NO_RESPONSE_RECOMMENDED'` (subject/body/language NULL, `reason`
    populated) for every other classification (REJECTION,
    APPLICATION_RECEIVED, WITHDRAWAL_OR_POSITION_CLOSED,
    AUTOMATED_NOTIFICATION, OTHER, UNKNOWN) — a safe, explicit,
    always-persisted "no" rather than silently generating nothing.

    **Never invents facts.** Generated `subject`/`body` text is built
    exclusively from already-trusted stored facts — `CandidateProfileRecord`
    fields that pass `is_top_level_fact_usable_for_generation`, and the
    matched `JobRecord`'s own title/company — never from the analyzed
    email's own subject/body/from_address (that content is untrusted
    correspondence text; see `app.agents.response_draft_generator`'s
    module docstring for why it is never even passed in). Anything the
    generator could not determine (candidate name, matched job, specific
    information a recruiter asked for, availability, salary/offer
    decisions) is listed in `missing_fields_json` and represented as an
    explicit bracketed placeholder in the body — never guessed.

    **Immutable, versioned, never UPDATEd** — same convention as
    `GmailMessageAnalysisRecord`. `UNIQUE(gmail_message_id, analysis_id,
    candidate_profile_version, generator_version)` is the idempotency
    identity: repeating the exact same generation request against the
    same analysis revision and the same candidate profile version returns
    the existing row; a later re-analysis (new `analysis_id`) or a
    candidate profile edit (`candidate_profile_version` bumped) produces
    a NEW revision instead, while every prior revision remains queryable.

    **Accepted limitation (documented, not engineered around in v1):**
    identity does not pin a `JobRecord` content fingerprint the way
    `CandidateCVDraftRecord`/`CandidateJobMatchRecord` pin
    `job_snapshot_fingerprint` — an edit to the matched job's
    title/company between two identical-looking generation calls will not
    by itself trigger a new revision. Job title/company change after
    initial collection is rare in practice (see `app.db.repositories.upsert_job`,
    which does not even update `url` on existing rows); a full
    content-fingerprint pin is deferred rather than spec'd speculatively.

    **`analysis_id`/`matched_job_id` are deliberately not ForeignKeys** —
    same "traceability, not identity" rationale as
    `GmailMessageAnalysisRecord.matched_job_id`'s own docstring: this row
    must remain a legible historical record even if the referenced
    analysis/job is later removed by some future maintenance path.
    `gmail_message_id` IS a real ForeignKey (`ondelete="CASCADE"`) —
    mirrors `GmailMessageAnalysisRecord.gmail_message_id`: a response
    draft has no meaning independent of the message it responds to.
    """

    __tablename__ = "response_drafts"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id",
            "analysis_id",
            "candidate_profile_version",
            "generator_version",
            name="uq_response_drafts_identity",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'NO_RESPONSE_RECOMMENDED')",
            name="ck_response_drafts_status_valid",
        ),
        CheckConstraint(
            "language IS NULL OR language IN ('de', 'en')",
            name="ck_response_drafts_language_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Denormalized from the parent gmail_messages row — same GMAIL-002-style
    # account-isolation convention as GmailMessageAnalysisRecord.account_key.
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    gmail_message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gmail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Not a ForeignKey — see class docstring.
    analysis_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Not a ForeignKey — see class docstring. None when the analysis this
    # draft is based on has no matched job (match_type != APPLICATION/JOB_ONLY).
    matched_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Copied from the pinned analysis for cheap filtering without a join.
    classification: Mapped[str] = mapped_column(String(40), nullable=False)

    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Populated only for status == 'NO_RESPONSE_RECOMMENDED'.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated only for status == 'PROPOSED'.
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Bounded JSON list of str — see class docstring's "never invents facts".
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # Provenance metadata (spec requirement) — always the deterministic
    # local generator in v1; no external/LLM provider exists yet.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Always True for every row this stage writes — see class docstring.
    # Not DB-CHECK-enforced (a fixed-True boolean CHECK is unusual in this
    # codebase's conventions, which reserve CHECK for enums/ranges); the
    # boundary is enforced by construction in
    # app/services/response_draft.py, which never passes False.
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ResponseDraftApprovalRecord(Base):
    """An immutable human APPROVE/REJECT decision on one exact,
    already-persisted `ResponseDraftRecord` revision (Stage 7D) — see
    app/services/response_draft_send.py for the orchestration that
    creates these and enforces "NO APPROVAL = NO SEND".

    **Pins the exact content being authorized.** `pinned_subject`/
    `pinned_body` are copied verbatim from the target `ResponseDraftRecord`
    at decision time — never re-read live from that row when a send is
    later attempted (see `ResponseDraftSendRecord`). `ResponseDraftRecord`
    rows are already immutable (never UPDATEd — see that model's
    docstring), so in the normal case these copies can never drift from
    the source; the pin exists as an explicit, auditable "this is exactly
    what the human saw and approved" anchor, and as defense in depth
    against a hypothetical future bug that reads the wrong revision at
    send time.

    **One decision per draft revision, ever — `UNIQUE(response_draft_id)`.**
    A human cannot "change their mind" on an already-decided revision;
    approving/rejecting an already-decided `response_draft_id` fails (see
    `app.db.response_draft_approval_repository.create_approval`) rather
    than silently overwriting the prior decision. This is deliberate, not
    a missing feature: Stage 7C's response-draft generation is itself
    idempotent-per-identity but produces a NEW revision whenever the
    underlying analysis/candidate-profile/generator version changes (see
    `ResponseDraftRecord`'s own docstring) — "I want to reconsider" is
    always expressed by generating and approving a NEW revision, which
    naturally gets its own fresh approval, never by mutating a past
    decision. This also means a REJECTED decision is permanent for that
    exact revision — never later flipped to APPROVED.

    Deliberately NOT linked to `GmailMessageAnalysisRecord`/`JobRecord`
    via a ForeignKey (same "traceability, not identity" rationale as
    `ResponseDraftRecord.analysis_id`/`matched_job_id`) — `gmail_message_id`
    here is a denormalized copy for query/account-scoping convenience
    only.
    """

    __tablename__ = "response_draft_approvals"
    __table_args__ = (
        UniqueConstraint("response_draft_id", name="uq_response_draft_approvals_response_draft"),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_response_draft_approvals_decision_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    response_draft_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("response_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized traceability only — see class docstring.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Verbatim copies of the approved ResponseDraftRecord's own
    # subject/body at decision time — see class docstring.
    pinned_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    pinned_body: Mapped[str] = mapped_column(Text, nullable=False)
    # Review finding 1.3: the reply recipient captured at APPROVAL time (the
    # inbound message's stored from_address, validated by
    # app.services.response_draft_recipient). "" = no pinned recipient: a
    # REJECTED decision, or an approval recorded before this column existed
    # -- such an approval can never be sent. The send re-derives the recipient
    # and refuses on any difference. Pinning proves nothing about who really
    # sent the inbound message (the header is forgeable); it fixes what the
    # operator authorized.
    pinned_recipient: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


# Stage 12: the send statuses that mean "a transmission is in flight, happened,
# or may have happened" -- i.e. every status except a DEFINITE
# pre-transmission FAILED. Shared by `ResponseDraftSendRecord`'s and
# `FollowUpSendRecord`'s per-target partial unique indexes below (and by the
# migration that creates them), so both tables use the identical predicate.
LIVE_SEND_STATUSES: tuple[str, ...] = ("PENDING", "SENT", "UNCERTAIN")


LIVE_SEND_STATUS_PREDICATE = "status IN ('PENDING', 'SENT', 'UNCERTAIN')"


class ResponseDraftSendRecord(Base):
    """The DB-enforced atomic arbiter of "has this approved response
    draft already been sent, or is a send currently in flight" (Stage
    7D) — see app/services/response_draft_send.py's module docstring for
    the full send-gate/idempotency contract this table is the concurrency
    backbone of.

    **Why this table exists (mirrors `GmailMessageIdClaimRecord`'s own
    "atomic arbiter via UNIQUE + INSERT/IntegrityError" pattern exactly).**
    A Python check-then-act ("is there already a SENT/PENDING row for
    this draft? if not, send") is racy: two concurrent send requests for
    the same approved draft could both observe "no row yet" and both
    proceed to call the outbound provider, resulting in the recruiter
    receiving the reply twice. `UNIQUE(response_draft_id)` closes that
    window structurally — only ONE row can ever exist for a given
    `response_draft_id`, and whichever concurrent request's INSERT
    commits first is the one that may actually call the provider; every
    other concurrent (or later) request instead reads the existing row
    and acts on ITS state (see `app.db.response_draft_approval_repository`'s
    `claim_send_attempt`/`retry_send_attempt`).

    **At most one live reply per inbound message, across ALL revisions
    (Stage 12).** `UNIQUE(response_draft_id)` alone only serializes sends
    of ONE revision: two different revisions of a draft for the same
    inbound message (e.g. regenerated after a candidate-profile edit) could
    each be approved and each sent, so the recruiter would receive two
    replies. `uq_response_draft_sends_live_per_message` -- a PARTIAL unique
    index `UNIQUE(account_key, gmail_message_id) WHERE status IN ('PENDING',
    'SENT', 'UNCERTAIN')` -- makes that impossible at the database level: at
    most one row per inbound message may ever be in flight, sent, or
    possibly sent. `FAILED` rows are excluded because a FAILED attempt is, by
    construction, a DEFINITE pre-transmission failure (see below), so it
    never blocks a later attempt (of the same or another revision). A claim
    INSERT or FAILED -> PENDING retry UPDATE that would violate the index is
    translated into `ResponseDraftMessageAlreadyRepliedError` by the
    repository -- never a silent second send.

    **`status` is a real, CAS-guarded state machine — mirrors
    `ApplicationPackageReviewRecord`'s own "deliberately mutable" precedent
    (the only other place in this project a row is UPDATEd in place rather
    than only ever inserted):**

    - `PENDING` — a send attempt is currently claimed/in flight. Set only
      by the INSERT that wins the UNIQUE-constraint race, or by a
      FAILED -> PENDING retry CAS (`WHERE id=:id AND status='FAILED'`,
      requiring exactly 1 row affected).
    - `SENT` — the outbound provider CONFIRMED success. Set only by a
      PENDING -> SENT CAS (`WHERE id=:id AND status='PENDING'`) executed
      AFTER `OutboundEmailProvider.send` returns without raising — never
      before (spec: "Do not mark SENT before provider success"). Once
      SENT, permanent: no code path ever transitions out of it.
    - `FAILED` — a DEFINITE pre-transmission failure only (auth/
      connection/message-construction — see
      `app.providers.email.outbound_base.EmailSendConnectionError`/
      `EmailSendAuthError`) — `send_message()` was never actually
      invoked, so "not sent" is provably true. Set only by a
      PENDING -> FAILED CAS, alongside `last_error`. A FAILED row does
      NOT consume the approval permanently — it may be retried
      (FAILED -> PENDING, `attempt_count` incremented), so one transient
      pre-send failure can never permanently block a legitimately
      approved reply from ever being sent, while still making it
      impossible for two concurrent retries to both win the same PENDING
      claim (the CAS `WHERE status='FAILED'` guard only ever lets one
      concurrent UPDATE affect a row).
    - `UNCERTAIN` — transmission was ATTEMPTED (the provider's
      `send_message()`-equivalent call was actually invoked) but an
      exception occurred before delivery could be confirmed OR ruled
      out — see `app.providers.email.outbound_base.EmailSendOutcomeUnknownError`'s
      docstring for the "safest-acceptable" classification rule this
      follows. Set only by a PENDING -> UNCERTAIN CAS, alongside
      `last_error`; `attempt_count` is left untouched (this is not a
      "failed attempt to be retried", it is a terminal, ambiguous
      outcome). **Fail-closed and terminal: NO code path ever
      transitions a row OUT of `UNCERTAIN`** — never automatically, and
      never via a later send request for the same draft (which is
      refused, provider never called again, before reaching this table's
      claim logic — see `app.services.response_draft_send`'s
      `ResponseDraftSendOutcomeUncertainError`). Manual reconciliation
      (checking whether the recruiter actually received the reply, and
      deciding what to do next) is intentionally out of scope for Stage
      7D — this table's job is only to make the ambiguity visible and
      prevent an automated duplicate-send risk, not to resolve it. Also
      reached (HARD-008, Codex master review) when a row is found
      PENDING with `send_attempted=True` on a later attempt — see
      `send_attempted` below — since a successful send whose SENT-commit
      itself then failed is indistinguishable, from a later reader's
      perspective, from a crash mid-send or a still-live concurrent
      attempt; all three fail closed to UNCERTAIN rather than ever being
      silently retried.

    **`send_attempted`** (HARD-008, Codex master review) durably
    distinguishes "transmission was never attempted for this claim"
    (safe to hand to a later request) from "transmission may already be
    underway or already happened" (never blindly retried) — see that
    column's own docstring below and
    `app.db.response_draft_approval_repository.begin_transmission`. This
    closes the gap where a `PENDING` row could previously be stranded
    forever with no recovery path if the outbound provider's `send()`
    call succeeded but the local commit recording `SENT` afterward
    failed (a DB connection drop or process kill at that exact instant).

    Deliberately NOT linked to `GmailMessageAnalysisRecord`/`JobRecord`
    via a ForeignKey, same rationale as `ResponseDraftApprovalRecord`.
    `approval_id` IS a real ForeignKey — a send attempt is meaningless
    without the approval that authorized it.
    """

    __tablename__ = "response_draft_sends"
    __table_args__ = (
        UniqueConstraint("response_draft_id", name="uq_response_draft_sends_response_draft"),
        Index(
            "uq_response_draft_sends_live_per_message",
            "account_key",
            "gmail_message_id",
            unique=True,
            sqlite_where=text(LIVE_SEND_STATUS_PREDICATE),
            postgresql_where=text(LIVE_SEND_STATUS_PREDICATE),
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_response_draft_sends_status_valid",
        ),
        CheckConstraint("attempt_count > 0", name="ck_response_draft_sends_attempt_count_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(
        String(320), nullable=False, server_default="", index=True
    )
    response_draft_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("response_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approval_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("response_draft_approvals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized traceability only — see ResponseDraftApprovalRecord's
    # class docstring for the same convention.
    gmail_message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # HARD-008 (Codex master review, crash/CAS recovery): False for the
    # entire window between winning the PENDING claim and the instant
    # right before `OutboundEmailProvider.send` is actually invoked;
    # flipped to True by a dedicated CAS
    # (`app.db.response_draft_approval_repository.begin_transmission`)
    # immediately before that call, in the same request that will make
    # it. A row found PENDING with `send_attempted=False` is PROVABLY
    # pre-transmission — the process that claimed it crashed (or never
    # got that far) before any network call was made, so it is always
    # safe to let a later request take over (see
    # `app.services.response_draft_send._resolve_existing_send_record`).
    # A row found PENDING with `send_attempted=True` means transmission
    # may already be underway (either a live concurrent request, a crash
    # mid-send, or a successful send whose SENT-recording commit itself
    # then failed) — indistinguishable from here, so it is NEVER retried;
    # instead it is moved to the fail-closed terminal `UNCERTAIN` state.
    # `begin_transmission`'s own CAS (`WHERE send_attempted=False`) is
    # what makes "who gets to actually call the provider" mutually
    # exclusive between concurrent requests — this column is the single
    # source of truth for that exclusivity, not `status` alone. Exact
    # mirror of `FollowUpSendRecord.send_attempted` (see that column's
    # own docstring, S7E-010).
    send_attempted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # RFC 5322 Message-ID the provider reported for the sent message, if
    # any — traceability only, never an identity/dedup key (mirrors
    # GmailMessageRecord.message_id_header's own convention). Set only on
    # SENT.
    provider_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    # Bounded, sanitized (str(exc), never a traceback/secret) — mirrors
    # CompanyResearchRecord.last_error's convention. Set only on FAILED.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set only on a CONFIRMED successful send — see class docstring.
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
