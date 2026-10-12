"""widen persisted Gmail IMAP UID and UIDVALIDITY columns to BIGINT

IMAP message UIDs and mailbox UIDVALIDITY are both RFC 3501 nz-numbers,
32-bit unsigned values 1..4294967295 (app.providers.email.base
.IMAP_NZ_NUMBER_MAX; the inbox parser accepts exactly that range), but six
Gmail persistence columns were created as signed INTEGER (max 2147483647),
so a legal UID or mailbox generation above that failed at PostgreSQL
INSERT time:

- gmail_messages.uid_validity, gmail_messages.uid
- gmail_message_id_claims.claimant_uid_validity,
  gmail_message_id_claims.claimant_uid
- gmail_permanent_skips.uid_validity, gmail_permanent_skips.uid

xing_scan_progress (uid_validity, confirmed_upto_uid) and
gmail_application_drafts (uid_validity, draft_uid) were already BIGINT and
are untouched.

Upgrade is INTEGER -> BIGINT only: every existing value is preserved
unchanged, and nullability, CHECK (`> 0`), unique constraints and indexes
are kept.

Downgrade narrows back to INTEGER but first checks ALL SIX columns and
REFUSES (raises, with counts only) if any value exceeds 2147483647, before
any column is altered, instead of failing midway or truncating.

Revision ID: e8b1c4f7a2d9
Revises: d2f5a8c1b7e4
Create Date: 2026-10-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b1c4f7a2d9"
down_revision: str | Sequence[str] | None = "d2f5a8c1b7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INTEGER_MAX = 2147483647
# table -> its UIDVALIDITY and UID columns (all NOT NULL).
_COLUMNS = {
    "gmail_messages": ("uid_validity", "uid"),
    "gmail_message_id_claims": ("claimant_uid_validity", "claimant_uid"),
    "gmail_permanent_skips": ("uid_validity", "uid"),
}


def _alter(table: str, columns: Sequence[str], from_type, to_type) -> None:
    with op.batch_alter_table(table) as batch_op:
        for column in columns:
            batch_op.alter_column(
                column, existing_type=from_type, type_=to_type, existing_nullable=False
            )


def upgrade() -> None:
    for table, columns in _COLUMNS.items():
        _alter(table, columns, sa.Integer(), sa.BigInteger())


def downgrade() -> None:
    bind = op.get_bind()
    too_large = {
        f"{table}.{column}": count
        for table, columns in _COLUMNS.items()
        for column in columns
        if (
            count := bind.execute(
                sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} > :limit"),
                {"limit": _INTEGER_MAX},
            ).scalar_one()
        )
    }
    if too_large:
        # Counts only -- never the stored values themselves.
        raise RuntimeError(
            "Refusing to narrow Gmail IMAP UID/UIDVALIDITY columns to INTEGER: rows exceed "
            f"{_INTEGER_MAX}: {too_large}"
        )
    for table, columns in _COLUMNS.items():
        _alter(table, columns, sa.BigInteger(), sa.Integer())
