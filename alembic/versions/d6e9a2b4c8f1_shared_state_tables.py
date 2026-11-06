"""Task 23: shared (cross-process) rate-limit and UI-session state tables

Additive on top of c5d8f1a3b7e2: `shared_rate_limit_hits` and `shared_ui_session_revocations`.
They are written ONLY when `SHARED_STATE_BACKEND=database`; with the default (`memory`) they stay
empty and nothing changes. The downgrade drops both tables (their content is transient).

Revision ID: d6e9a2b4c8f1
Revises: c5d8f1a3b7e2
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d6e9a2b4c8f1"
down_revision: str | Sequence[str] | None = "c5d8f1a3b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shared_rate_limit_hits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("limiter", sa.String(length=64), nullable=False),
        sa.Column("identity", sa.String(length=64), nullable=False),
        sa.Column("hit_at", sa.Float(), nullable=False),
        sqlite_autoincrement=True,  # ids are refund tokens: never reuse a deleted id
    )
    op.create_index(
        "ix_shared_rate_limit_hits_identity",
        "shared_rate_limit_hits",
        ["limiter", "identity", "hit_at"],
    )
    op.create_index(
        "ix_shared_rate_limit_hits_limiter_time",
        "shared_rate_limit_hits",
        ["limiter", "hit_at"],
    )
    op.create_table(
        "shared_ui_session_revocations",
        sa.Column("sid", sa.String(length=32), primary_key=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_shared_ui_session_revocations_expires_at",
        "shared_ui_session_revocations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shared_ui_session_revocations_expires_at", table_name="shared_ui_session_revocations"
    )
    op.drop_table("shared_ui_session_revocations")
    op.drop_index("ix_shared_rate_limit_hits_limiter_time", table_name="shared_rate_limit_hits")
    op.drop_index("ix_shared_rate_limit_hits_identity", table_name="shared_rate_limit_hits")
    op.drop_table("shared_rate_limit_hits")
