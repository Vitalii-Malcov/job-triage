"""Stage 22 P1: structured source facts on jobs; STRETCH fit tier

Additive only, on top of d5f1a7c3e9b2: nine nullable `jobs` columns for the
facts the Bundesagentur search result states -- workplace coordinates,
postcode, region and number of locations; home-office flag, type and
percentage; the "suitable for career changers" flag. No existing row is
changed; the next collector run fills them. Plain ALTER TABLE like
b2c5d8e4f7a1 / d5f1a7c3e9b2 (no batch rebuild of `jobs` under its FKs). The
downgrade drops the columns (the values come back with the next run).

The fit-tier CHECK of the Stage 21 cache `job_fit_assessments` gains
STRETCH. SQLite cannot alter a CHECK in place, so the cache table is dropped
and recreated (both directions) -- it holds only derived rows that
`scripts/stage21_assess_vacancies.py` recomputes; every row would be stale
after this release anyway (new job columns and new rules are in its key).

Revision ID: e3b7c9d1f4a6
Revises: d5f1a7c3e9b2
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3b7c9d1f4a6"
down_revision: str | Sequence[str] | None = "d5f1a7c3e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("workplace_lat", sa.Float()),
    ("workplace_lon", sa.Float()),
    ("workplace_postal_code", sa.String(10)),
    ("workplace_region", sa.String(40)),
    ("workplace_count", sa.Integer()),
    ("home_office_possible", sa.Boolean()),
    ("home_office_type", sa.String(40)),
    ("home_office_percent", sa.Integer()),
    ("quereinstieg_suitable", sa.Boolean()),
)


OLD_TIERS = "'GOOD_MATCH', 'PARTIAL_MATCH', 'NEEDS_OPERATOR', 'NOT_SUITABLE'"
NEW_TIERS = "'GOOD_MATCH', 'PARTIAL_MATCH', 'STRETCH', 'NEEDS_OPERATOR', 'NOT_SUITABLE'"


def _recreate_cache(tiers: str) -> None:
    """Same table as c7e2a9d4f1b3, with the given fit-tier list."""
    op.drop_index("ix_job_fit_assessments_input_key", table_name="job_fit_assessments")
    op.drop_index("ix_job_fit_assessments_tier_job", table_name="job_fit_assessments")
    op.drop_table("job_fit_assessments")
    op.create_table(
        "job_fit_assessments",
        sa.Column("job_id", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("fit_tier", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=False),
        sa.Column("tier_reasons_json", sa.Text(), nullable=False),
        sa.Column("work_mode", sa.String(30), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"fit_tier IN ({tiers})", name="ck_job_fit_assessments_fit_tier"),
        sa.CheckConstraint(
            "decision IN ('BLOCKED', 'NEEDS_VERIFICATION', 'CANDIDATE')",
            name="ck_job_fit_assessments_decision",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index(
        "ix_job_fit_assessments_tier_job", "job_fit_assessments", ["fit_tier", "job_id"]
    )
    op.create_index("ix_job_fit_assessments_input_key", "job_fit_assessments", ["input_key"])


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("jobs", sa.Column(name, type_, nullable=True))
    _recreate_cache(NEW_TIERS)


def downgrade() -> None:
    _recreate_cache(OLD_TIERS)
    for name, _type in reversed(COLUMNS):
        op.drop_column("jobs", name)
