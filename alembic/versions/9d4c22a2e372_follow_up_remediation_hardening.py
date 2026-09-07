"""follow up remediation hardening (S7E-007/008/009/010)

Stage 7E Codex remediation round (1 HIGH + 10 MEDIUM blockers) — adds the
columns backing:

- S7E-008 (recipient safety): `follow_up_proposals.recipient` /
  `follow_up_approvals.pinned_recipient` — a validated, single canonical
  external recipient derived from trusted outbound provenance, pinned at
  approval time exactly like `pinned_subject`/`pinned_body` already are.
- S7E-009 (proposal staleness): `follow_up_proposals.input_fingerprint` —
  folded into that table's UNIQUE identity (replacing
  `uq_follow_up_proposals_anchor`) so a re-evaluation of the same anchor
  after any trusted input changed produces a NEW proposal revision instead
  of silently reusing a stale one.
- S7E-010 (crash/CAS recovery): `follow_up_sends.send_attempted` — lets a
  later request distinguish a PENDING row that is PROVABLY pre-transmission
  (safe to take over) from one where transmission may already be underway
  (fail-closed to UNCERTAIN, never blindly retried).

See app/db/models.py's `FollowUpProposalRecord` / `FollowUpApprovalRecord`
/ `FollowUpSendRecord` docstrings for the full rationale.

Revision ID: 9d4c22a2e372
Revises: c8a2f4e6b1d3
Create Date: 2026-09-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4c22a2e372"
down_revision: str | Sequence[str] | None = "c8a2f4e6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() before any Stage 7E DDL runs — see
    `_preflight_check_downgrade_is_safe`'s docstring. Duplicated verbatim
    from alembic/versions/7058c097a542_gmail_account_scope_and_hardening.py
    rather than imported: this project's convention (see e.g.
    app/db/follow_up_repository.py's own docstring) is to duplicate small,
    self-contained Alembic downgrade-preflight helpers across migration
    modules rather than reach into another migration's private symbols —
    migration files are not meant to import from one another.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """The SAME account-scope compatibility preflight
    7058c097a542.downgrade() runs, reused here (S7E-007 downgrade safety):
    a downgrade chain that passes through THIS migration on its way past
    7058c097a542 must not proceed at all — leaving revision, tables, and
    data entirely untouched — if the underlying `gmail_messages`/
    `gmail_threads` data is not representable by the pre-account-scoping
    schema. Running it here, first, before any of this migration's own
    column/constraint changes are touched, means a doomed deep downgrade
    fails at the very top of the chain rather than partway through, and
    this migration's downgrade() never leaves Stage 7E's own tables in an
    inconsistent half-migrated state only to fail later at 7058c097a542
    anyway.
    """
    message_conflicts = connection.execute(
        sa.text(
            """
            SELECT mailbox, uid_validity, uid, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_messages
            GROUP BY mailbox, uid_validity, uid
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if message_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 9d4c22a2e372: {len(message_conflicts)} "
            "gmail_messages (mailbox, uid_validity, uid) identity/identities "
            "are shared by more than one account_key, which a downgrade past "
            "7058c097a542 later in this same chain cannot represent. Resolve "
            "manually before downgrading — this migration will not do so "
            "implicitly."
        )

    thread_conflicts = connection.execute(
        sa.text(
            """
            SELECT thread_key, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_threads
            GROUP BY thread_key
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if thread_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 9d4c22a2e372: {len(thread_conflicts)} "
            "gmail_threads thread_key value(s) are shared by more than one "
            "account_key, which a downgrade past 7058c097a542 later in this "
            "same chain cannot represent. Resolve manually before "
            "downgrading — this migration will not do so implicitly."
        )


class FollowUpProposalDowngradeConflict(RuntimeError):
    """Raised by downgrade() (alongside `GmailAccountScopeDowngradeConflict`
    above) when collapsing `uq_follow_up_proposals_anchor_fingerprint` back
    to the pre-S7E-009 `uq_follow_up_proposals_anchor` would violate the
    OLD (fingerprint-less) UNIQUE constraint — i.e. more than one proposal
    revision now legitimately exists for the same anchor (exactly the
    staleness fix's intended behavior: a changed trusted input creates a
    NEW revision rather than reusing a stale one). The old schema cannot
    represent more than one row per anchor at all, so downgrading must
    never silently delete/merge the extra revision(s).
    """


def _preflight_check_proposal_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    conflicts = connection.execute(
        sa.text(
            """
            SELECT account_key, anchor_gmail_message_id, COUNT(*) AS revisions
            FROM follow_up_proposals
            GROUP BY account_key, anchor_gmail_message_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if conflicts:
        raise FollowUpProposalDowngradeConflict(
            f"Cannot downgrade past 9d4c22a2e372: {len(conflicts)} "
            "(account_key, anchor_gmail_message_id) pair(s) have more than "
            "one follow_up_proposals revision (S7E-009 staleness fix: a "
            "changed trusted input creates a new revision per anchor). The "
            "pre-fingerprint schema's UNIQUE(account_key, "
            "anchor_gmail_message_id) constraint cannot represent this "
            "without deleting/merging revisions. Resolve manually before "
            "downgrading — this migration will not do so implicitly."
        )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("follow_up_proposals") as batch_op:
        batch_op.add_column(
            sa.Column("recipient", sa.String(length=320), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("input_fingerprint", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.drop_constraint("uq_follow_up_proposals_anchor", type_="unique")
        batch_op.create_unique_constraint(
            "uq_follow_up_proposals_anchor_fingerprint",
            ["account_key", "anchor_gmail_message_id", "input_fingerprint"],
        )

    with op.batch_alter_table("follow_up_approvals") as batch_op:
        batch_op.add_column(
            sa.Column("pinned_recipient", sa.String(length=320), nullable=False, server_default="")
        )

    with op.batch_alter_table("follow_up_sends") as batch_op:
        batch_op.add_column(
            sa.Column("send_attempted", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema.

    S7E-007: the account-scope compatibility preflight runs FIRST — see
    `_preflight_check_downgrade_is_safe`. If it raises, nothing below has
    executed yet: schema, data, and the recorded Alembic revision are all
    untouched.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())
    _preflight_check_proposal_downgrade_is_safe(op.get_bind())

    with op.batch_alter_table("follow_up_sends") as batch_op:
        batch_op.drop_column("send_attempted")

    with op.batch_alter_table("follow_up_approvals") as batch_op:
        batch_op.drop_column("pinned_recipient")

    with op.batch_alter_table("follow_up_proposals") as batch_op:
        batch_op.drop_constraint("uq_follow_up_proposals_anchor_fingerprint", type_="unique")
        batch_op.create_unique_constraint(
            "uq_follow_up_proposals_anchor", ["account_key", "anchor_gmail_message_id"]
        )
        batch_op.drop_column("input_fingerprint")
        batch_op.drop_column("recipient")
