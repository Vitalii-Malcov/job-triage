"""add telegram_bewerbung_preparations table (Stage 9B Telegram Bewerbung draft)

Adds `telegram_bewerbung_preparations` -- the Stage 9B workflow ledger, one
row per `telegram_vacancy_reviews.id` (see
app.db.models.TelegramBewerbungPreparationRecord's docstring). Workflow
metadata only: the 6B/6C/6D artifacts it pins (`match_id`/`cv_draft_id`/
`bewerbung_draft_id`) are immutable snapshot rows referenced WITHOUT foreign
keys, exactly like the existing snapshot tables, so deleting the ledger (or
the review/job it cascades from) never deletes a draft artifact. Only
`review_id` is a real FK, with ON DELETE CASCADE, because the ledger is
operational state of that review row and nothing else.

No existing table is altered; no backfill (a row is created on the first
"Bewerbung erstellen" press).

Revision ID: d7b3e9a2c5f1
Revises: c4e8a1f63b9d
Create Date: 2026-10-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7b3e9a2c5f1"
down_revision: str | Sequence[str] | None = "c4e8a1f63b9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "telegram_bewerbung_preparations"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "review_id",
            sa.Integer(),
            sa.ForeignKey("telegram_vacancy_reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_identity", sa.String(length=64), nullable=False),
        sa.Column("prep_claim_token", sa.String(length=32), nullable=True),
        sa.Column("prep_claim_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("match_id", sa.Integer(), nullable=True),
        sa.Column("cv_draft_id", sa.Integer(), nullable=True),
        sa.Column("bewerbung_draft_id", sa.Integer(), nullable=True),
        sa.Column("package_token", sa.String(length=32), nullable=True),
        sa.Column("preview_text", sa.Text(), nullable=True),
        sa.Column("preview_renderer_version", sa.String(length=30), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("preview_state", sa.String(length=20), nullable=False, server_default="NONE"),
        sa.Column("preview_claim_token", sa.String(length=32), nullable=True),
        sa.Column("preview_claim_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preview_message_id", sa.BigInteger(), nullable=True),
        sa.Column("preview_sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("review_id", name="uq_telegram_bewerbung_preparations_review_id"),
        sa.UniqueConstraint(
            "package_token", name="uq_telegram_bewerbung_preparations_package_token"
        ),
        sa.CheckConstraint(
            "state IN ('PREPARING', 'PREPARED', 'FAILED')",
            name="ck_telegram_bewerbung_preparations_state_valid",
        ),
        sa.CheckConstraint(
            "generation >= 1", name="ck_telegram_bewerbung_preparations_generation_positive"
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_telegram_bewerbung_preparations_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "(state = 'PREPARING' AND prep_claim_token IS NOT NULL "
            "AND prep_claim_started_at IS NOT NULL) "
            "OR (state <> 'PREPARING' AND prep_claim_token IS NULL)",
            name="ck_telegram_bewerbung_preparations_prep_token_state",
        ),
        sa.CheckConstraint(
            "state <> 'PREPARED' OR (match_id IS NOT NULL AND cv_draft_id IS NOT NULL "
            "AND bewerbung_draft_id IS NOT NULL AND package_token IS NOT NULL "
            "AND preview_text IS NOT NULL AND preview_renderer_version IS NOT NULL)",
            name="ck_telegram_bewerbung_preparations_prepared_artifacts",
        ),
        sa.CheckConstraint(
            "preview_state IN ('NONE', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_telegram_bewerbung_preparations_preview_state_valid",
        ),
        sa.CheckConstraint(
            "(preview_state = 'SENDING' AND preview_claim_token IS NOT NULL "
            "AND preview_claim_started_at IS NOT NULL) "
            "OR (preview_state <> 'SENDING' AND preview_claim_token IS NULL)",
            name="ck_telegram_bewerbung_preparations_preview_token_state",
        ),
        sa.CheckConstraint(
            "preview_state <> 'SENDING' OR state = 'PREPARED'",
            name="ck_telegram_bewerbung_preparations_preview_requires_prepared",
        ),
    )
    op.create_index(
        "ix_telegram_bewerbung_preparations_state_claim",
        _TABLE,
        ["state", "prep_claim_started_at"],
    )
    op.create_index(
        "ix_telegram_bewerbung_preparations_preview_claim",
        _TABLE,
        ["preview_state", "preview_claim_started_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_telegram_bewerbung_preparations_preview_claim", table_name=_TABLE)
    op.drop_index("ix_telegram_bewerbung_preparations_state_claim", table_name=_TABLE)
    op.drop_table(_TABLE)
