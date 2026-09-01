"""gmail account scope and integrity hardening

Stage 7A security remediation round (Codex review GMAIL-002/GMAIL-009):
adds account-scoped identity to the Gmail inbox tables and DB-level
CHECK constraints, on top of the original Stage 7A schema
(8634f4be953a). Applied as a NEW migration rather than editing
8634f4be953a in place — Stage 7A is not yet merged to main, but the
prior migration is already committed to this feature branch's history,
and this project's convention (every stage/change ships its own
migration, history is never rewritten — see README "Миграции") favors a
follow-up migration over amending a committed one.

Revision ID: 7058c097a542
Revises: 8634f4be953a
Create Date: 2026-08-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7058c097a542"
down_revision: str | Sequence[str] | None = "8634f4be953a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() (GMAIL-013 fix, added after this migration
    was first committed — acceptable here specifically because Stage 7A
    is not yet merged and this is a downgrade-safety fix to THIS
    migration's own downgrade path, not a schema/history rewrite) when
    collapsing account-scoped identity back to the pre-GMAIL-002 schema
    would violate the old (account_key-less) UNIQUE constraints.

    Account-scoped identity (this migration's upgrade()) legitimately
    allows two different accounts to share the same
    (mailbox, uid_validity, uid) or thread_key — e.g. two Gmail accounts
    both named "INBOX" with UIDVALIDITY/UID values that happen to
    coincide. The pre-account-scoping schema's UNIQUE constraints have no
    account_key column at all, so it cannot represent both rows without
    silently merging two different accounts' correspondence into one
    identity — which downgrade() must never do implicitly.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Raised BEFORE any DDL runs (see downgrade() below) — a failed
    preflight must leave the schema, data, and Alembic revision entirely
    untouched: no `_alembic_tmp_*` table, no partial batch-mode table
    rebuild, nothing to clean up.
    """
    message_conflicts = connection.execute(
        sa.text(
            """
            SELECT mailbox, uid_validity, uid, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_messages
            GROUP BY mailbox, uid_validity, uid
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if message_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 7058c097a542: {len(message_conflicts)} "
            "gmail_messages (mailbox, uid_validity, uid) identity/identities "
            "are shared by more than one account_key. The pre-account-scoping "
            "schema (8634f4be953a) cannot represent this without merging "
            "distinct accounts' correspondence into one row. Resolve manually "
            "(e.g. re-scope the affected accounts to distinct mailboxes, or "
            "remove the redundant rows) before downgrading — this migration "
            "will not do so implicitly."
        )

    thread_conflicts = connection.execute(
        sa.text(
            """
            SELECT thread_key, COUNT(DISTINCT account_key) AS accounts
            FROM gmail_threads
            GROUP BY thread_key
            HAVING COUNT(DISTINCT account_key) > 1
            """
        )
    ).fetchall()
    if thread_conflicts:
        raise GmailAccountScopeDowngradeConflict(
            f"Cannot downgrade past 7058c097a542: {len(thread_conflicts)} "
            "gmail_threads thread_key value(s) are shared by more than one "
            "account_key. The pre-account-scoping schema (8634f4be953a) "
            "cannot represent this without merging distinct accounts' "
            "correspondence threads into one row. Resolve manually before "
            "downgrading — this migration will not do so implicitly."
        )


def upgrade() -> None:
    """Upgrade schema."""
    # `server_default=""` backfills every pre-existing row (Stage 7A has
    # no production data yet — this branch is unmerged — so "" is a safe,
    # explicit "predates account scoping" placeholder, not a guess at a
    # real account) on both SQLite and PostgreSQL without a separate
    # UPDATE statement.
    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.add_column(
            sa.Column("account_key", sa.String(length=320), server_default="", nullable=False)
        )
        batch_op.drop_constraint("uq_gmail_threads_thread_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_threads_account_thread_key", ["account_key", "thread_key"]
        )
        batch_op.create_index("ix_gmail_threads_account_key", ["account_key"])

    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.add_column(
            sa.Column("account_key", sa.String(length=320), server_default="", nullable=False)
        )
        batch_op.drop_constraint("uq_gmail_messages_provider_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_messages_account_provider_identity",
            ["account_key", "mailbox", "uid_validity", "uid"],
        )
        batch_op.create_index("ix_gmail_messages_account_key", ["account_key"])
        batch_op.create_check_constraint("ck_gmail_messages_uid_positive", "uid > 0")
        batch_op.create_check_constraint(
            "ck_gmail_messages_uid_validity_positive", "uid_validity > 0"
        )
        batch_op.create_check_constraint(
            "ck_gmail_messages_direction_valid", "direction IN ('INBOUND', 'OUTBOUND')"
        )


def downgrade() -> None:
    """Downgrade schema.

    GMAIL-013: a deterministic preflight runs BEFORE any DDL — see
    `_preflight_check_downgrade_is_safe`. If it raises, nothing below has
    executed yet: schema, data, and the recorded Alembic revision are all
    untouched.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    with op.batch_alter_table("gmail_messages") as batch_op:
        batch_op.drop_constraint("ck_gmail_messages_direction_valid", type_="check")
        batch_op.drop_constraint("ck_gmail_messages_uid_validity_positive", type_="check")
        batch_op.drop_constraint("ck_gmail_messages_uid_positive", type_="check")
        batch_op.drop_index("ix_gmail_messages_account_key")
        batch_op.drop_constraint("uq_gmail_messages_account_provider_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_messages_provider_identity", ["mailbox", "uid_validity", "uid"]
        )
        batch_op.drop_column("account_key")

    with op.batch_alter_table("gmail_threads") as batch_op:
        batch_op.drop_index("ix_gmail_threads_account_key")
        batch_op.drop_constraint("uq_gmail_threads_account_thread_key", type_="unique")
        batch_op.create_unique_constraint("uq_gmail_threads_thread_key", ["thread_key"])
        batch_op.drop_column("account_key")
