"""add xing_scan_progress table (Codex gate follow-up, Astra R4A starvation)

Adds `xing_scan_progress` -- a single durable row (keyed by `source`,
currently always `"xing"`) tracking the highest IMAP UID that
`app.collectors.xing_email.XingEmailCollector` has confirmed is safe to
never re-examine, plus the mailbox's `UIDVALIDITY` at the time that UID
was observed. See app.db.models.XingScanProgressRecord's docstring for
the full rationale, including why an IMAP UID is safe to use as a
watermark where AUD-004 already proved a PostgreSQL row id is not
(migration aff3c7dc6349).

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
        sa.Column("uid_validity", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_upto_uid", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", name="uq_xing_scan_progress_source"),
    )
    op.create_index("ix_xing_scan_progress_source", "xing_scan_progress", ["source"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_xing_scan_progress_source", table_name="xing_scan_progress")
    op.drop_table("xing_scan_progress")
