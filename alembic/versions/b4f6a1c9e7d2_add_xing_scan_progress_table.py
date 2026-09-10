"""add xing_scan_progress table (Codex gate follow-up, Astra R4A starvation)

Adds `xing_scan_progress` -- a durable row per (`source`, `mailbox_scope`)
-- `source` currently always `"xing"`, `mailbox_scope` a deterministic
non-secret hash of the configured mailbox's (host, port, username,
mailbox) identity (see
app.db.xing_scan_progress_repository.compute_mailbox_scope) -- tracking
the highest IMAP UID that
`app.collectors.xing_email.XingEmailCollector` has confirmed is safe to
never re-examine, plus the mailbox's `UIDVALIDITY` at the time that UID
was observed. See app.db.models.XingScanProgressRecord's docstring for
the full rationale, including why an IMAP UID is safe to use as a
watermark where AUD-004 already proved a PostgreSQL row id is not
(migration aff3c7dc6349).

`mailbox_scope` (Codex gate follow-up, Astra R4A MEDIUM: mailbox scope)
was folded into this still-unmerged migration rather than shipped as a
separate follow-up migration -- this table has no prior release to stay
compatible with, so there is no reason to carry the migration debt of a
single-column-keyed table that briefly existed. Without it, a `source`
-only key would let two DIFFERENT mailboxes that happen to share a
`UIDVALIDITY` value silently reuse each other's watermark and skip an
unscanned UID prefix in the new mailbox -- see `compute_mailbox_scope`'s
own docstring.

This table is new progress-cursor state only -- it does not touch
`processed_email_messages` or `jobs`.

Revision ID: b4f6a1c9e7d2
Revises: aff3c7dc6349
Create Date: 2026-09-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f6a1c9e7d2"
down_revision: str | Sequence[str] | None = "aff3c7dc6349"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "xing_scan_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("mailbox_scope", sa.String(length=64), nullable=False),
        sa.Column("uid_validity", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_upto_uid", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", "mailbox_scope", name="uq_xing_scan_progress_source_mailbox"),
    )
    op.create_index("ix_xing_scan_progress_source", "xing_scan_progress", ["source"], unique=False)
    op.create_index(
        "ix_xing_scan_progress_mailbox_scope", "xing_scan_progress", ["mailbox_scope"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_xing_scan_progress_mailbox_scope", table_name="xing_scan_progress")
    op.drop_index("ix_xing_scan_progress_source", table_name="xing_scan_progress")
    op.drop_table("xing_scan_progress")
