"""add telegram_vacancy_reviews table (Stage 9A Telegram vacancy feed)

Adds `telegram_vacancy_reviews` -- one row per `jobs.id` holding that
vacancy's Telegram-feed review state (see
app.db.models.TelegramVacancyReviewRecord's docstring). New bookkeeping
table only: `jobs` (including `jobs.status`, the application lifecycle) is
not touched.

`uq_telegram_vacancy_reviews_job_id` is what makes "the same logical
vacancy is never carded twice" a database guarantee rather than a Python
check. No backfill: existing jobs get a row the next time a collector
re-sees them.

Revision ID: 9a1f4c7e2b3d
Revises: b2c5d8e4f7a1
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a1f4c7e2b3d"
down_revision: str | Sequence[str] | None = "b2c5d8e4f7a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "telegram_vacancy_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("callback_token", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("job_id", name="uq_telegram_vacancy_reviews_job_id"),
        sa.UniqueConstraint("callback_token", name="uq_telegram_vacancy_reviews_callback_token"),
        sa.CheckConstraint(
            "state IN ("
            "'DISCOVERED', 'QUEUED_FOR_REVIEW', 'SENDING', 'TELEGRAM_SENT', "
            "'DELIVERY_UNCERTAIN', 'DELIVERY_FAILED', 'SAVED', 'SKIPPED')",
            name="ck_telegram_vacancy_reviews_state_valid",
        ),
    )
    op.create_index(
        "ix_telegram_vacancy_reviews_state",
        "telegram_vacancy_reviews",
        ["state"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_telegram_vacancy_reviews_state", table_name="telegram_vacancy_reviews")
    op.drop_table("telegram_vacancy_reviews")
