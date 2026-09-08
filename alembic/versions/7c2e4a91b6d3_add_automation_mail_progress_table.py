"""add automation_mail_progress table (Stage 8D crash-safe cursors)

Adds `automation_mail_progress` -- one persisted row per account tracking
how far the Gmail response-draft cycle and the follow-up proposal cycle
have each progressed (see app.db.models.AutomationMailProgressRecord's
docstring for the full rationale). This table is new progress-cursor
state only -- it does not touch `automation_runs` (Stage 8A),
`gmail_messages` (Stage 7A), or `jobs`.

Concurrency: `uq_automation_mail_progress_account_key` enforces one row
per account. The actual "an old, lease-losing run can't clobber a newer
owner's progress" guarantee comes from
`app.db.automation_mail_progress_repository.advance_gmail_cursor`/
`advance_follow_up_cursor`'s plain conditional `UPDATE ... WHERE
account_key = ... AND <cursor column> = :expected` -- ordinary portable
SQL, mirroring `automation_schedules`' `claim_due_schedule` CAS from
4fb941b18aff.

Revision ID: 7c2e4a91b6d3
Revises: 4fb941b18aff
Create Date: 2026-09-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e4a91b6d3"
down_revision: str | Sequence[str] | None = "4fb941b18aff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "automation_mail_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("gmail_after_message_id", sa.Integer(), nullable=True),
        sa.Column("follow_up_after_job_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("account_key", name="uq_automation_mail_progress_account_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("automation_mail_progress")
