"""enforce at most one live send per inbound message / follow-up anchor

Stage 12 (controlled Gmail response workflow). `response_draft_sends` and
`follow_up_sends` were each unique only per draft/proposal REVISION
(`UNIQUE(response_draft_id)` / `UNIQUE(follow_up_proposal_id)`), so two
revisions targeting the same inbound message (or the same follow-up anchor)
could each be approved and each be sent -- a duplicate email to the
recruiter. This adds a PARTIAL unique index to each table:

- `uq_response_draft_sends_live_per_message`:
  `response_draft_sends(account_key, gmail_message_id)`
- `uq_follow_up_sends_live_per_anchor`:
  `follow_up_sends(account_key, gmail_message_id)` (the anchor message)

both `WHERE status IN ('PENDING', 'SENT', 'UNCERTAIN')` -- every status
except a definite pre-transmission `FAILED`. The predicate is declared for
both SQLite and PostgreSQL (same convention as
`uq_automation_runs_one_running_per_account`).

Upgrade first counts existing rows that would violate either index and
REFUSES (raises, with counts only -- never message identities) before
creating anything, instead of failing midway on the second index. No row is
modified or deleted: resolving a pre-existing duplicate live send is a
human decision. Downgrade drops both indexes; no data is touched.

If creating an index fails on SQLite (non-transactional DDL), the indexes
this invocation already created are dropped before the error is re-raised,
so the database stays at e8b1c4f7a2d9 with no partial state and the upgrade
can simply be retried. Indexes that existed before the invocation are never
dropped.

Revision ID: a7c3e9f1b2d4
Revises: e8b1c4f7a2d9
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1b2d4"
down_revision: str | Sequence[str] | None = "e8b1c4f7a2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.db.models.LIVE_SEND_STATUS_PREDICATE -- a migration must
# not import application code that may change after it ships.
_LIVE_PREDICATE = "status IN ('PENDING', 'SENT', 'UNCERTAIN')"

# index name -> table
_INDEXES = {
    "uq_response_draft_sends_live_per_message": "response_draft_sends",
    "uq_follow_up_sends_live_per_anchor": "follow_up_sends",
}


def upgrade() -> None:
    bind = op.get_bind()
    conflicts = {
        table: count
        for table in _INDEXES.values()
        if (
            count := bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM ("
                    f"SELECT account_key, gmail_message_id FROM {table} "
                    f"WHERE {_LIVE_PREDICATE} "
                    "GROUP BY account_key, gmail_message_id HAVING COUNT(*) > 1"
                    ") AS duplicate_targets"
                )
            ).scalar_one()
        )
    }
    if conflicts:
        # Counts only -- never account keys or message ids.
        raise RuntimeError(
            "Refusing to add live-send uniqueness: targets already have more than one "
            f"PENDING/SENT/UNCERTAIN send row: {conflicts}. Resolve them manually first."
        )
    created: list[tuple[str, str]] = []
    try:
        for name, table in _INDEXES.items():
            op.create_index(
                name,
                table,
                ["account_key", "gmail_message_id"],
                unique=True,
                sqlite_where=sa.text(_LIVE_PREDICATE),
                postgresql_where=sa.text(_LIVE_PREDICATE),
            )
            created.append((name, table))
    except Exception:
        # S12-002 (Codex review): SQLite DDL is not transactional here, so an
        # index created before the failure would survive while the revision
        # stays e8b1c4f7a2d9, and every retry would then fail on "already
        # exists". Undo only what THIS invocation created, then re-raise.
        # PostgreSQL rolls the whole migration back itself (and its aborted
        # transaction would refuse the DROP anyway).
        if not op.get_context().impl.transactional_ddl:
            _drop_indexes_created_by_this_upgrade(created)
        raise


def _drop_indexes_created_by_this_upgrade(created: list[tuple[str, str]]) -> None:
    try:
        for name, table in reversed(created):
            op.drop_index(name, table_name=table)
    except Exception as cleanup_error:
        names = [name for name, _table in created]
        raise RuntimeError(
            f"live-send uniqueness upgrade failed and could not remove the index(es) it "
            f"had just created: {names}. Drop them manually before retrying."
        ) from cleanup_error


def downgrade() -> None:
    for name, table in _INDEXES.items():
        op.drop_index(name, table_name=table)
