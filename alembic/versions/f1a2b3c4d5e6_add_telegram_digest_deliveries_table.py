"""add telegram_digest_deliveries table (Stage 8E daily digest idempotency)

Adds `telegram_digest_deliveries` -- one persisted row per (account_key,
digest_date) delivery attempt of the optional DAILY Telegram digest (see
app.db.models.TelegramDigestDeliveryRecord's docstring for the full
idempotency/CAS rationale). This table is new delivery-bookkeeping state
only -- it does not touch `automation_runs`, `automation_schedules`, or
any Gmail/response-draft/follow-up table.

Concurrency: `uq_telegram_digest_deliveries_account_date` enforces at most
one delivery row per account per local calendar date. The actual
"a restart or a second concurrent scheduler worker can't send the same
day's digest twice" guarantee comes from
`app.db.telegram_digest_repository.claim_delivery`'s INSERT +
IntegrityError-catch idiom (mirrors
`app.db.response_draft_approval_repository.claim_send_attempt`), plus
`retry_delivery`/`mark_sent`/`mark_failed`/`mark_uncertain`'s CAS UPDATEs
conditioned on `id` + expected `status`.

Revision ID: f1a2b3c4d5e6
Revises: 7c2e4a91b6d3
Create Date: 2026-09-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "7c2e4a91b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "telegram_digest_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "account_key", "digest_date", name="uq_telegram_digest_deliveries_account_date"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'UNCERTAIN')",
            name="ck_telegram_digest_deliveries_status_valid",
        ),
    )
    op.create_index(
        "ix_telegram_digest_deliveries_account_key",
        "telegram_digest_deliveries",
        ["account_key"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_telegram_digest_deliveries_account_key", table_name="telegram_digest_deliveries"
    )
    op.drop_table("telegram_digest_deliveries")
