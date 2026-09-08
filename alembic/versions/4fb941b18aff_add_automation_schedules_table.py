"""add automation_schedules table (Stage 8B scheduler foundation)

Adds `automation_schedules` -- one persisted row per account tracking
when its next scheduled `app.services.automation.run_automation_cycle`
cycle is due (see app.db.models.AutomationScheduleRecord's docstring for
the full rationale). This table is new schedule-arbitration state only
-- it does not touch `automation_runs` (Stage 8A) beyond an informational
`last_run_id` pointer.

Concurrency: `uq_automation_schedules_account_key` enforces one row per
account. The actual multi-process "at most one claimer wins a due slot"
guarantee comes from `app.db.automation_schedule_repository.claim_due_schedule`'s
plain conditional `UPDATE ... WHERE account_key = ... AND next_run_at =
:observed AND next_run_at <= :now` -- ordinary portable SQL, not a
dialect-specific construct, so no `sqlite_where`/`postgresql_where` split
is needed here (unlike `automation_runs`' partial unique index in
d4a7e1c3f8b2) -- see tests/test_automation_schedule_migration.py for the
dialect-compatibility proof.

Revision ID: 4fb941b18aff
Revises: d4a7e1c3f8b2
Create Date: 2026-09-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4fb941b18aff"
down_revision: str | Sequence[str] | None = "d4a7e1c3f8b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "automation_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_run_id",
            sa.Integer(),
            sa.ForeignKey("automation_runs.id"),
            nullable=True,
        ),
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
        sa.UniqueConstraint("account_key", name="uq_automation_schedules_account_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("automation_schedules")
