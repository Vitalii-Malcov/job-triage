"""add telegram_vacancy_reviews.claim_token (Stage 9A, Codex S9A-CODEX-004)

Adds a nullable `claim_token` -- the identity of the row's current `SENDING`
claim (see app.db.models.TelegramVacancyReviewRecord). Every transition out
of `SENDING` matches on it, so a stale worker can never release or resolve a
newer worker's claim. NULL outside `SENDING`, so existing rows need no
backfill. A row left in `SENDING` by a pre-upgrade process has a NULL token
and therefore matches no token-bound transition; only the existing stale
reconciliation (`SENDING -> DELIVERY_UNCERTAIN`) can resolve it -- the
conservative outcome.

Revision ID: c4e8a1f63b9d
Revises: 9a1f4c7e2b3d
Create Date: 2026-10-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1f63b9d"
down_revision: str | Sequence[str] | None = "9a1f4c7e2b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("telegram_vacancy_reviews", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claim_token", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("telegram_vacancy_reviews", schema=None) as batch_op:
        batch_op.drop_column("claim_token")
