"""add automation_runs table (Stage 8A orchestrator foundation)

Adds `automation_runs` — one persisted row per orchestrated job-search
cycle (see app.services.automation.run_automation_cycle and
app.db.models.AutomationRunRecord's docstring for the full rationale).

Concurrency: `uq_automation_runs_one_running_per_account` is a PARTIAL
unique index (`UNIQUE(account_key) WHERE status = 'RUNNING'`) — the
database-enforced arbiter of "at most one RUNNING run per account at a
time". SQLite supports partial indexes natively via `WHERE` on
`CREATE INDEX`; this migration creates it directly rather than through
`op.create_index`'s cross-dialect helper, mirroring how CheckConstraint
below also targets this project's SQLite-only deployment target (see
alembic/versions/7058c097a542_gmail_account_scope_and_hardening.py and
similar prior migrations, none of which support any other dialect).

Revision ID: d4a7e1c3f8b2
Revises: c9e2a4f7b1d6
Create Date: 2026-09-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7e1c3f8b2"
down_revision: str | Sequence[str] | None = "c9e2a4f7b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("results_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_automation_runs_status_valid",
        ),
    )
    op.create_index(
        "ix_automation_runs_account_key", "automation_runs", ["account_key"], unique=False
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_automation_runs_one_running_per_account "
        "ON automation_runs (account_key) WHERE status = 'RUNNING'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS uq_automation_runs_one_running_per_account")
    op.drop_index("ix_automation_runs_account_key", table_name="automation_runs")
    op.drop_table("automation_runs")
