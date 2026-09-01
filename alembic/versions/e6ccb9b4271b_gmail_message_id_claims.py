"""gmail message-id ownership claims

Stage 7A security remediation round 2 (Codex review GMAIL-011): a
DB-enforced atomic arbiter for "which provider message legitimately owns
this Message-ID within this account" — replaces a Python
check-then-act collision guard that was itself racy under concurrency.
See app/db/models.py's GmailMessageIdClaimRecord docstring for the full
rationale, and app/db/gmail_repository.py's
`_claim_message_id_or_get_collision_thread` for how the UNIQUE
constraint below is used as the actual concurrency arbiter.

Revision ID: e6ccb9b4271b
Revises: 7058c097a542
Create Date: 2026-09-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6ccb9b4271b"
down_revision: str | Sequence[str] | None = "7058c097a542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() (GMAIL-013) when collapsing account-scoped
    identity back to the pre-GMAIL-002 schema (8634f4be953a) would
    violate that schema's account_key-less UNIQUE constraints. See
    7058c097a542's own `GmailAccountScopeDowngradeConflict` for the full
    rationale — this is the SAME check, duplicated here deliberately
    (not imported from 7058c097a542) rather than shared, for two
    reasons: (1) this migration's revision id starts with a hex digit,
    which is not a syntactically valid Python module name to `import`
    directly, making any cross-migration import fragile/non-standard;
    (2) Alembic version scripts are conventionally self-contained and
    never depend on each other's internals — only on `down_revision`
    ordering — so a future edit to one migration's downgrade can never
    silently break another's.

    Without this migration's OWN preflight, a downgrade starting from
    CURRENT HEAD (e6ccb9b4271b) would run in this order: (1) this
    migration's downgrade() drops gmail_message_id_claims and commits,
    (2) alembic_version advances to 7058c097a542, (3) THAT migration's
    own preflight finally detects the conflict and raises. Step 1 and 2
    would already have mutated the database by the time the failure
    surfaces — violating "database unchanged on unsafe downgrade". This
    migration's preflight runs first specifically to prevent that.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Identical read-only compatibility check to
    7058c097a542's own `_preflight_check_downgrade_is_safe` — see that
    function's docstring and this module's `GmailAccountScopeDowngradeConflict`
    for why it is duplicated here rather than imported. Must run BEFORE
    any DDL in downgrade() below.
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
    op.create_table(
        "gmail_message_id_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=320), nullable=False),
        sa.Column("message_id_header", sa.String(length=998), nullable=False),
        sa.Column("claimant_mailbox", sa.String(length=100), nullable=False),
        sa.Column("claimant_uid_validity", sa.Integer(), nullable=False),
        sa.Column("claimant_uid", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("contested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["gmail_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key",
            "message_id_header",
            name="uq_gmail_message_id_claims_account_message_id",
        ),
        sa.CheckConstraint("claimant_uid > 0", name="ck_gmail_message_id_claims_uid_positive"),
        sa.CheckConstraint(
            "claimant_uid_validity > 0",
            name="ck_gmail_message_id_claims_uid_validity_positive",
        ),
    )
    op.create_index(
        op.f("ix_gmail_message_id_claims_thread_id"),
        "gmail_message_id_claims",
        ["thread_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    GMAIL-013: runs the same account-scope compatibility preflight as
    7058c097a542's own downgrade() — BEFORE this migration's own DDL —
    so a downgrade starting from current HEAD fails closed before
    anything (including this migration's own claims-table drop) has
    mutated the database. See `_preflight_check_downgrade_is_safe` and
    `GmailAccountScopeDowngradeConflict` above for why this check is
    duplicated here rather than shared via import.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    op.drop_index(
        op.f("ix_gmail_message_id_claims_thread_id"), table_name="gmail_message_id_claims"
    )
    op.drop_table("gmail_message_id_claims")
