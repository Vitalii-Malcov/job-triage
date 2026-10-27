"""Stage 17: search targets and the origin coordinates of car routes

Additive only, on top of a9d3f6b2c4e8 (which is left exactly as published):

- `candidate_job_preferences.target_seniority` (JUNIOR | UNKNOWN; feeds the
  Stage 11A collector seniority gate when JUNIOR) and
  `candidate_job_preferences.search_keywords_json` (Bundesagentur search
  terms; empty = BUNDESAGENTUR_SEARCH_KEYWORDS);
- `job_commute_checks.origin_lat` / `origin_lon`: the coordinates a car
  route was measured from, so a route from a moved origin is detected as
  stale.

Existing rows keep their meaning: UNKNOWN / [] / NULL (a route without
origin coordinates only matches a configuration without coordinates).
Downgrade refuses while any of these columns holds a non-default value
(configuration and verification data are never silently discarded);
otherwise it drops exactly these columns.

Revision ID: b6c1e9a4d2f7
Revises: a9d3f6b2c4e8
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6c1e9a4d2f7"
down_revision: str | Sequence[str] | None = "a9d3f6b2c4e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate_job_preferences",
        sa.Column("target_seniority", sa.String(20), nullable=False, server_default="UNKNOWN"),
    )
    op.add_column(
        "candidate_job_preferences",
        sa.Column("search_keywords_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column("job_commute_checks", sa.Column("origin_lat", sa.Float(), nullable=True))
    op.add_column("job_commute_checks", sa.Column("origin_lon", sa.Float(), nullable=True))


_POPULATED = (
    (
        "candidate_job_preferences",
        "target_seniority <> 'UNKNOWN' OR search_keywords_json <> '[]'",
    ),
    ("job_commute_checks", "origin_lat IS NOT NULL OR origin_lon IS NOT NULL"),
)


def downgrade() -> None:
    bind = op.get_bind()
    for table, populated in _POPULATED:
        rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {populated}")).scalar()
        if rows:
            raise RuntimeError(
                f"cannot downgrade: {table} holds {rows} row(s) with Stage 17 values; "
                "restore a pre-migration backup instead of discarding them"
            )
    with op.batch_alter_table("job_commute_checks") as batch_op:
        batch_op.drop_column("origin_lon")
        batch_op.drop_column("origin_lat")
    with op.batch_alter_table("candidate_job_preferences") as batch_op:
        batch_op.drop_column("search_keywords_json")
        batch_op.drop_column("target_seniority")
