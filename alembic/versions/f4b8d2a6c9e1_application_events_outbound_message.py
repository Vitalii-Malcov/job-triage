"""allow OUTBOUND_MESSAGE application events (candidate service mail)

Adds the event type OUTBOUND_MESSAGE to `application_events`: a service
message the candidate already sent about an existing application (e.g. a
phone-number correction). It never changes the status, so its shape is
`old_status IS NULL AND new_status IS NULL`.

Only two CHECK constraints change (`ck_application_events_type_valid`,
`ck_application_events_shape`); no column, row or index is touched.

- PostgreSQL: the two constraints are dropped and re-created in place; the
  append-only trigger is unaffected.
- SQLite: constraints can only change by rebuilding the table (Alembic batch
  mode). The rebuild drops the table's triggers, so the append-only triggers
  of c8e4a2f6d1b9 are re-created right after it. Existing rows are copied
  unchanged (ids, sequences, correlation keys).

Downgrade refuses while any OUTBOUND_MESSAGE row exists (application events
are append-only, so such rows can never be removed to satisfy the old
constraint); otherwise it restores the previous constraints.

Revision ID: f4b8d2a6c9e1
Revises: e3a9c5d7f2b4
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4b8d2a6c9e1"
down_revision: str | Sequence[str] | None = "e3a9c5d7f2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen vocabularies -- a migration must not import the live enums.
_OLD_EVENT_TYPES = ("CREATED", "STATUS_CHANGED", "STATUS_OVERRIDDEN", "INBOUND_MESSAGE")
_NEW_EVENT_TYPES = (*_OLD_EVENT_TYPES, "OUTBOUND_MESSAGE")
_OLD_SHAPE = (
    "(event_type = 'CREATED' AND old_status IS NULL AND new_status IS NOT NULL) OR "
    "(event_type IN ('STATUS_CHANGED', 'STATUS_OVERRIDDEN') "
    "AND old_status IS NOT NULL AND new_status IS NOT NULL) OR "
    "(event_type = 'INBOUND_MESSAGE' AND new_status IS NULL)"
)
_NEW_SHAPE = (
    _OLD_SHAPE
    + " OR (event_type = 'OUTBOUND_MESSAGE' AND old_status IS NULL AND new_status IS NULL)"
)
_TYPE_CHECK = "ck_application_events_type_valid"
_SHAPE_CHECK = "ck_application_events_shape"

# Frozen copy of c8e4a2f6d1b9's SQLite triggers (re-created after a rebuild).
_MESSAGE = "application_events is append-only"
_SQLITE_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_application_events_no_update",
    "DROP TRIGGER IF EXISTS trg_application_events_no_delete",
    "CREATE TRIGGER trg_application_events_no_update BEFORE UPDATE ON application_events "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
    "CREATE TRIGGER trg_application_events_no_delete BEFORE DELETE ON application_events "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _swap_checks(event_types: tuple[str, ...], shape: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_constraint(_TYPE_CHECK, "application_events", type_="check")
        op.drop_constraint(_SHAPE_CHECK, "application_events", type_="check")
        op.create_check_constraint(
            _TYPE_CHECK, "application_events", _in("event_type", event_types)
        )
        op.create_check_constraint(_SHAPE_CHECK, "application_events", shape)
        return
    if dialect != "sqlite":
        raise RuntimeError(f"unsupported dialect for application_events: {dialect}")
    with op.batch_alter_table("application_events", recreate="always") as batch_op:
        batch_op.drop_constraint(_TYPE_CHECK, type_="check")
        batch_op.drop_constraint(_SHAPE_CHECK, type_="check")
        batch_op.create_check_constraint(_TYPE_CHECK, _in("event_type", event_types))
        batch_op.create_check_constraint(_SHAPE_CHECK, shape)
    for statement in _SQLITE_TRIGGERS:
        op.execute(statement)


def upgrade() -> None:
    _swap_checks(_NEW_EVENT_TYPES, _NEW_SHAPE)


def downgrade() -> None:
    outbound = (
        op.get_bind()
        .execute(
            sa.text("SELECT COUNT(*) FROM application_events WHERE event_type = 'OUTBOUND_MESSAGE'")
        )
        .scalar()
    )
    if outbound:
        raise RuntimeError(
            f"cannot downgrade: {outbound} OUTBOUND_MESSAGE event(s) exist and application "
            "events are append-only; restore a pre-migration backup instead"
        )
    _swap_checks(_OLD_EVENT_TYPES, _OLD_SHAPE)
