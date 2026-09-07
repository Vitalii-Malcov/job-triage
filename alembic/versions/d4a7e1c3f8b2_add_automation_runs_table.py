"""add automation_runs table (Stage 8A orchestrator foundation)

Adds `automation_runs` — one persisted row per orchestrated job-search
cycle (see app.services.automation.run_automation_cycle and
app.db.models.AutomationRunRecord's docstring for the full rationale).

Concurrency: `uq_automation_runs_one_running_per_account` is a PARTIAL
unique index (`UNIQUE(account_key) WHERE status = 'RUNNING'`) — the
database-enforced arbiter of "at most one RUNNING run per account at a
time". S8A-001 (Codex re-review): created via `op.create_index`'s
dialect-specific `sqlite_where`/`postgresql_where` kwargs (both set to
the SAME predicate) rather than a raw, SQLite-only `CREATE INDEX ...
WHERE` string — this project runs on SQLite today, but
`app.core.config.Settings.database_url` already documents PostgreSQL as
a supported target (see the `.[postgres]` extra), so this migration (and
`app.db.models.AutomationRunRecord`'s ORM `Index(...)`, which mirrors
this exactly) must describe equivalent semantics on both — see
tests/test_automation_run_index_dialects.py for the compiled-DDL proof.

Crash recovery: `lease_holder`/`lease_expires_at` (S8A-002, Codex
re-review) are an ownership-aware lease mirroring Stage 7E's
`gmail_threads.lock_holder`/`lock_expires_at` — see
`app.db.models.AutomationRunRecord`'s docstring for the full rationale.
Added directly to this table's initial creation (not a follow-up
migration) since this table has not shipped to `main` yet.

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
        sa.Column("lease_holder", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "uq_automation_runs_one_running_per_account",
        "automation_runs",
        ["account_key"],
        unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"),
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_automation_runs_one_running_per_account", table_name="automation_runs")
    op.drop_index("ix_automation_runs_account_key", table_name="automation_runs")
    op.drop_table("automation_runs")
