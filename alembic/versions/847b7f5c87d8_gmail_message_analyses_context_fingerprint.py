"""gmail message analyses context fingerprint

Stage 7B Codex remediation round 1 (7B-003/004): adds
`gmail_message_analyses.context_fingerprint` and widens the table's
idempotency UNIQUE constraint to include it.

**Why.** The prior identity —
`(gmail_message_id, analysis_version, input_fingerprint)` — covered only
the MESSAGE's own content. A Codex review reproduced that re-analyzing an
unchanged message after (a) a correct `JobRecord` was added, or (b) the
same thread gained a new decisive prior match, silently returned the OLD,
now-stale cached analysis — because none of those external changes touch
`input_fingerprint`. `context_fingerprint` (see
app.db.gmail_analysis_repository.compute_context_fingerprint) is a
SHA-256 digest over the EFFECTIVE candidate `JobRecord` pool and thread
prior-match context an analysis run actually considered; when either
changes, a fresh analyze call now produces a genuinely NEW revision
instead of reusing a stale one, while every prior revision remains
queryable (this table is still never UPDATEd).

Purely additive to THIS table's own schema — no other table's schema or
data is touched, and no existing `gmail_message_analyses` row becomes
invalid (existing rows backfill `context_fingerprint=''`, which cannot
collide with any real SHA-256 hex digest, so no accidental duplicate
identity is created for previously-persisted rows).

**Downgrade still runs e6ccb9b4271b/7058c097a542's account-scope
preflight first anyway (duplicated here — same GMAIL-013 rationale as
813c9d5086d0's own docstring explains one link earlier in the chain).**
Without it, downgrading from THIS migration's head would drop/alter this
table's own columns and commit before ever reaching e6ccb9b4271b's own
preflight, once again mutating the database ahead of a failure that is
supposed to leave it untouched.

Revision ID: 847b7f5c87d8
Revises: 813c9d5086d0
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "847b7f5c87d8"
down_revision: str | Sequence[str] | None = "813c9d5086d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() when collapsing account-scoped identity back
    to the pre-GMAIL-002 schema (8634f4be953a) would violate that
    schema's account_key-less UNIQUE constraints. Same check as
    813c9d5086d0/e6ccb9b4271b's own `GmailAccountScopeDowngradeConflict`
    — duplicated here deliberately (not imported), for the same reasons
    those classes' docstrings give: hex-leading revision ids aren't
    importable Python module names, and Alembic version scripts are
    conventionally self-contained.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Identical read-only compatibility check to 813c9d5086d0/
    e6ccb9b4271b's own `_preflight_check_downgrade_is_safe` — see this
    module's own docstring for why it is duplicated here rather than
    imported. Must run BEFORE any DDL in downgrade() below.
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
    with op.batch_alter_table("gmail_message_analyses", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_fingerprint", sa.String(length=64), nullable=False, server_default=""
            )
        )
        batch_op.drop_constraint("uq_gmail_message_analyses_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_message_analyses_identity",
            ["gmail_message_id", "analysis_version", "input_fingerprint", "context_fingerprint"],
        )


def downgrade() -> None:
    """Downgrade schema.

    Runs the same account-scope compatibility preflight as
    813c9d5086d0/e6ccb9b4271b/7058c097a542's own downgrade() — BEFORE
    this migration's own DDL — so a downgrade starting from current HEAD
    fails closed before anything (including this migration's own column
    drop) has mutated the database. See
    `_preflight_check_downgrade_is_safe` and
    `GmailAccountScopeDowngradeConflict` above, and this module's own
    docstring, for why this check is duplicated here rather than shared.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    with op.batch_alter_table("gmail_message_analyses", schema=None) as batch_op:
        batch_op.drop_constraint("uq_gmail_message_analyses_identity", type_="unique")
        batch_op.create_unique_constraint(
            "uq_gmail_message_analyses_identity",
            ["gmail_message_id", "analysis_version", "input_fingerprint"],
        )
        batch_op.drop_column("context_fingerprint")
