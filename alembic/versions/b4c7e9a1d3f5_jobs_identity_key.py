"""Task 20: jobs.identity_key groups the distinguished siblings of one XING identity

Additive on top of a2b7d4f9c1e3: `jobs.identity_key` (String(64), NULL, indexed).
Nothing is back-filled and no existing row is rewritten; legacy rows keep NULL and
stay members of their own base group through `fingerprint`.

Downgrade REFUSES (before any DDL) while distinguished sibling rows exist (identity_key
differs from fingerprint): dropping the column would strand them, because their unique
fingerprints would stay taken while no group lookup could find them again. Nothing is
deleted or merged automatically.

Revision ID: b4c7e9a1d3f5
Revises: a2b7d4f9c1e3
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4c7e9a1d3f5"
down_revision: str | Sequence[str] | None = "a2b7d4f9c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_jobs_identity_key"


def upgrade() -> None:
    op.add_column("jobs", sa.Column("identity_key", sa.String(length=64), nullable=True))
    op.create_index(_INDEX, "jobs", ["identity_key"], unique=False)


def downgrade() -> None:
    sibling = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM jobs WHERE identity_key IS NOT NULL "
                "AND identity_key <> fingerprint LIMIT 1"
            )
        )
        .first()
    )
    if sibling is not None:
        raise RuntimeError(
            "Cannot downgrade: jobs holds distinguished XING sibling rows that only "
            "identity_key can group. Resolve them manually first (nothing was changed)."
        )
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_column("identity_key")
