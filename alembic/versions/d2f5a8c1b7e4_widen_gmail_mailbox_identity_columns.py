"""widen Gmail canonical mailbox identity columns to String(600)

Codex mailbox-width finding: Gmail mailboxes are persisted as their
canonical modified-UTF-7 identity (ASTRA-GMAIL-001-R1), and a mailbox name
the config accepts (at most 100 decoded code points) can encode to up to
536 ASCII characters -- e.g. 40 x "Я" is 109. String(100) made such a
valid mailbox fail only at PostgreSQL INSERT time. See
app.db.models.GMAIL_MAILBOX_IDENTITY_MAX_LENGTH for the derivation; the
literals are repeated here because a migration never imports app code
(tests compare the copies).

The same identity is also embedded in `gmail_threads.lock_holder`
("gmail_sync:<identity>:<uid_validity>:<uid>:<uuid hex>", at most 666
characters), so that String(200) column is widened to String(700) here
too (app.db.models.GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH).

Upgrade only widens VARCHAR columns: on PostgreSQL that is a catalog-only
change (no table rewrite, no value change), and every unique constraint
and index on these columns is kept as-is.

Downgrade narrows back but first REFUSES (raises) if any stored value is
longer than the old width, instead of letting the narrowing fail midway
or truncate a value.

Revision ID: d2f5a8c1b7e4
Revises: f3b8d2a6c9e1
Create Date: 2026-10-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f5a8c1b7e4"
down_revision: str | Sequence[str] | None = "f3b8d2a6c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAILBOX_OLD_LENGTH = 100
_MAILBOX_NEW_LENGTH = 600
_LOCK_HOLDER_OLD_LENGTH = 200
_LOCK_HOLDER_NEW_LENGTH = 700
# (table, column, nullable, old length, new length)
_COLUMNS = (
    ("gmail_messages", "mailbox", False, _MAILBOX_OLD_LENGTH, _MAILBOX_NEW_LENGTH),
    (
        "gmail_message_id_claims",
        "claimant_mailbox",
        False,
        _MAILBOX_OLD_LENGTH,
        _MAILBOX_NEW_LENGTH,
    ),
    ("gmail_permanent_skips", "mailbox", False, _MAILBOX_OLD_LENGTH, _MAILBOX_NEW_LENGTH),
    ("gmail_threads", "lock_holder", True, _LOCK_HOLDER_OLD_LENGTH, _LOCK_HOLDER_NEW_LENGTH),
)


def _alter(table: str, column: str, nullable: bool, from_length: int, to_length: int) -> None:
    with op.batch_alter_table(table) as batch_op:
        batch_op.alter_column(
            column,
            existing_type=sa.String(length=from_length),
            type_=sa.String(length=to_length),
            existing_nullable=nullable,
        )


def upgrade() -> None:
    for table, column, nullable, old_length, new_length in _COLUMNS:
        _alter(table, column, nullable, old_length, new_length)


def downgrade() -> None:
    bind = op.get_bind()
    too_long = {
        f"{table}.{column}": count
        for table, column, _, old_length, _ in _COLUMNS
        if (
            count := bind.execute(
                sa.text(f"SELECT COUNT(*) FROM {table} WHERE LENGTH({column}) > :limit"),
                {"limit": old_length},
            ).scalar_one()
        )
    }
    if too_long:
        # Counts only -- never the stored values themselves.
        raise RuntimeError(
            "Refusing to narrow Gmail mailbox identity columns: rows would be "
            f"truncated or rejected: {too_long}"
        )
    for table, column, nullable, old_length, new_length in _COLUMNS:
        _alter(table, column, nullable, new_length, old_length)
