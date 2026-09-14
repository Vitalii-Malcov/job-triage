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


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("response_draft_sends") as batch_op:
        batch_op.add_column(
            sa.Column("send_attempted", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("response_draft_sends") as batch_op:
        batch_op.drop_column("send_attempted")
