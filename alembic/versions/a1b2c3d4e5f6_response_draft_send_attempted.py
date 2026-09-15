"""response draft send transmission-boundary hardening (HARD-008)

Codex master review: `app.services.response_draft_send.send_response_draft`
persisted PENDING before calling SMTP with no durable marker for "the
irreversible send call is about to happen / has happened" -- if the
outbound provider's `send()` succeeded but the subsequent
`mark_send_sent()` commit failed (DB connection drop, process kill),
the row stayed PENDING forever with no recovery path, even though the
real email had already been transmitted.

Adds `response_draft_sends.send_attempted` -- the exact same crash/CAS
recovery column `follow_up_sends.send_attempted` already has (see
migration `9d4c22a2e372`) and the exact same rationale:
`app.db.response_draft_approval_repository.begin_transmission` CASes
this False -> True immediately before the outbound provider is ever
called; a row found PENDING with `send_attempted=False` is PROVABLY
pre-transmission (safe to reclaim); a row found PENDING with
`send_attempted=True` means transmission may already be underway or may
have already crashed mid-flight -- never retried automatically, always
reconciled to the fail-closed terminal `UNCERTAIN` state instead. See
`app.db.models.ResponseDraftSendRecord`'s docstring for the full
rationale.

**HARD-008-RR1 (Codex targeted re-review): the backfill for rows that
already existed BEFORE this migration ran.** The column itself is added
with `server_default=false` (so a bare `ALTER TABLE ... ADD COLUMN`
gives every pre-existing row `send_attempted=False` for free) -- correct
for `SENT`/`FAILED`/`UNCERTAIN` rows (see below) but UNSAFE left as-is
for `PENDING` rows, and is corrected by the two `op.execute` backfills
below.

A legacy `PENDING` row predates `begin_transmission`'s CAS entirely --
before this migration, nothing durably recorded whether the outbound
provider had ever actually been invoked for it. A `PENDING` row found by
the OLD code could mean any of: (1) the claim was made and the process
crashed before calling the provider at all (genuinely safe to retry),
(2) the provider was called, raised, and the process crashed before
`mark_send_failed`/`mark_send_uncertain` committed, or (3) the provider
ALREADY SUCCEEDED and the process crashed before the `mark_send_sent`
commit landed -- i.e. the literal HARD-008 scenario this whole column
exists to catch. Case (3) is indistinguishable from cases (1)/(2) by
inspecting the row alone, so backfilling a legacy `PENDING` row to
`send_attempted=False` (what the bare `ADD COLUMN ... server_default`
would otherwise leave it as) would let the new recovery logic in
`app.services.response_draft_send._resolve_existing_send_record` treat
it as PROVABLY pre-transmission and hand it to a later request to
re-send -- reproducing the exact duplicate-send failure HARD-008 exists
to prevent, except now triggered BY the migration meant to fix it.

Every legacy `PENDING` row is therefore migrated directly to the
fail-closed terminal `UNCERTAIN` status (never automatically retried,
never handed back to `_resolve_existing_send_record`'s reclaim branch --
see that function's `record.status == "UNCERTAIN"` case, which always
raises `ResponseDraftSendOutcomeUncertainError` instead of resending),
with `send_attempted=True` (it is NOT provably pre-transmission) and a
`last_error` marker identifying this migration as the source of the
transition, so a human reconciling `UNCERTAIN` rows post-upgrade can
tell "flagged by HARD-008-RR1's backfill" apart from "flagged by an
actual runtime crash". This is the ambiguity-already-exists-at-upgrade-
time case, so migrating straight to the terminal state (rather than
`PENDING + send_attempted=True`, which only becomes safe if every
recovery path deterministically reconciles it to `UNCERTAIN` before any
provider call -- true today, but a strictly larger trust surface than
just emitting the already-correct terminal state directly) is preferred.

Legacy `SENT`/`FAILED`/`UNCERTAIN` rows are backfilled to
`send_attempted=True` (not left at the column's own `False` default)
purely for row-level consistency with what actually happened, but this
has no behavioral effect on any of the three: `SENT` and `UNCERTAIN` are
already terminal (`_resolve_existing_send_record` raises immediately on
`status` alone, before ever consulting `send_attempted`), and a legacy
`FAILED` row's retry path (`retry_send_attempt`) unconditionally resets
`send_attempted` back to `False` as part of its own `FAILED -> PENDING`
CAS regardless of the value backfilled here -- legitimate `FAILED`
retry semantics are therefore unaffected either way.

Rows created AFTER this migration runs are untouched by the two
`op.execute` backfills below (they only ever run once, at upgrade time)
and still get the column's own `server_default=false` /
`ResponseDraftSendRecord.send_attempted`'s ORM `default=False` --
`claim_send_attempt`'s fresh INSERTs are unaffected, exactly as before
this re-review.

Revision ID: a1b2c3d4e5f6
Revises: c7d3f9a1e5b8
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "c7d3f9a1e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# HARD-008-RR1: identifies rows whose UNCERTAIN status/last_error came
# from this migration's fail-closed legacy-PENDING backfill, not from an
# actual runtime send outcome -- see module docstring.
_LEGACY_PENDING_BACKFILL_MARKER = (
    "HARD-008-RR1: legacy PENDING row predates the send_attempted column "
    "and is not provably pre-transmission; migrated fail-closed to "
    "UNCERTAIN by a1b2c3d4e5f6 instead of left automatically retryable."
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("response_draft_sends") as batch_op:
        batch_op.add_column(
            sa.Column("send_attempted", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    # HARD-008-RR1 backfill (see module docstring for the full
    # per-status rationale): every row that already existed before this
    # migration ran is, at minimum, marked send_attempted=True -- true by
    # construction for SENT/FAILED/UNCERTAIN, and the only fail-closed
    # choice for PENDING (never provably pre-transmission).
    op.execute("UPDATE response_draft_sends SET send_attempted = TRUE")
    # A legacy PENDING row is never provably pre-transmission -- migrate
    # it straight to the fail-closed terminal UNCERTAIN state rather than
    # leaving it PENDING (see module docstring for why PENDING here,
    # even with send_attempted=True, is a strictly larger trust surface
    # than emitting the terminal state directly).
    op.execute(
        sa.text(
            "UPDATE response_draft_sends "
            "SET status = 'UNCERTAIN', last_error = :marker "
            "WHERE status = 'PENDING'"
        ).bindparams(marker=_LEGACY_PENDING_BACKFILL_MARKER)
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("response_draft_sends") as batch_op:
        batch_op.drop_column("send_attempted")
