"""job reference tokens

Stage 7B Codex remediation round 2 (Blocker 3): adds
`job_reference_tokens`, normalized job/application reference tokens for
exact-equality lookup — replaces the `LIKE '%token%'` substring recall
`app.db.gmail_analysis_repository.get_job_candidates` previously used,
which a Codex review reproduced as starvable (enough OTHER jobs'
url/title merely CONTAINING pieces of a searched token could fill the
targeted query's LIMIT with false partial collisions before the real
exact match was ever retrieved). See app.db.models.JobReferenceTokenRecord
and app.db.repositories.sync_job_reference_tokens for the full rationale
and the single write path.

**Backfill.** Existing `JobRecord` rows are backfilled by computing
tokens with a extraction routine DUPLICATED (not imported) from
`app.services.email_matching.extract_reference_tokens` as it existed at
this revision — see `_extract_reference_tokens` below and Round 3
(Blocker R3-003)'s note on why. Purely additive/derived data; if this
migration is ever re-run against the same `jobs` data it recomputes
identical tokens (deterministic function, no randomness).

**Round 3 (Blocker R3-003): this migration must not import mutable
runtime business code.** An earlier version of this file imported
`app.services.email_matching.extract_reference_tokens` directly inside
`upgrade()`. A Codex review flagged this as unsafe for an immutable
historical migration: a future change to that runtime module's matching
logic (a bug fix, a behavior change, or even just a rename/refactor)
would silently change what a REPLAY of this already-shipped migration
produces, or break the replay outright. Migrations must stay
self-contained and deterministic forever, independent of how the live
application evolves. The small extraction routine this backfill needs is
therefore duplicated verbatim (as of this revision) below, using only
stable infrastructure (`re`, stdlib) — never `app.services.*` or any
other mutable application import.

**Downgrade still runs e6ccb9b4271b/7058c097a542's account-scope
preflight first anyway (duplicated here — same GMAIL-013 rationale as
813c9d5086d0/847b7f5c87d8's own docstrings explain earlier in the
chain).** `job_reference_tokens` itself has nothing to do with Gmail
account scoping, but the established project invariant is "downgrading
from whatever HEAD currently is fails closed, in full, before ANY DDL
belonging to a migration newer than the account-scope hardening runs" —
so this migration's downgrade() preserves that chain-wide guarantee too.

Revision ID: 805108385946
Revises: 847b7f5c87d8
Create Date: 2026-09-02

"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "805108385946"
down_revision: str | Sequence[str] | None = "847b7f5c87d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Self-contained reference-token extraction (Round 3, Blocker R3-003).
#
# This logic intentionally duplicates the Stage 7B reference-token
# extraction semantics as of revision 805108385946 so historical migration
# replay remains deterministic if runtime code later changes. Do NOT import
# app.services.email_matching (or any other app.* module) here — see the
# module docstring above for the full rationale. Keep this block a
# self-contained, frozen snapshot; if the runtime extraction semantics
# change later, that is a NEW behavior for NEW code, not a retroactive
# change to what this migration computed when it ran.
# ---------------------------------------------------------------------------

_MAX_REFERENCE_TOKENS = 10

_REFERENCE_PATTERN = re.compile(
    r"\b(?:referenz(?:nummer)?|kennziffer|ref(?:erenz)?(?:[-\s]?nr\.?)?|job[-\s]?id|"
    r"stellen[-\s]?(?:nr\.?|id)|vacancy[-\s]?id|requisition[-\s]?id)"
    r"[:\s#]+([A-Za-z0-9][A-Za-z0-9\-/]{2,19})",
    re.IGNORECASE,
)
_URL_ID_PATTERN = re.compile(r"/([A-Za-z0-9][A-Za-z0-9\-]{2,19})(?=[/?]|$)")
_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[\w.\-/?=&%]*)?", re.IGNORECASE
)


def _extract_url_ids(url_text: str) -> set[str]:
    return {
        match.group(1).upper()
        for match in _URL_ID_PATTERN.finditer(url_text)
        if any(char.isdigit() for char in match.group(1))
    }


def _extract_urls(text: str) -> list[str]:
    return _URL_PATTERN.findall(text)[:_MAX_REFERENCE_TOKENS]


def _extract_reference_tokens(text: str, url: str = "") -> frozenset[str]:
    """Frozen duplicate of `app.services.email_matching.extract_reference_tokens`
    as of revision 805108385946 — see module docstring and the block
    comment above for why this is duplicated rather than imported.
    """
    tokens = {match.group(1).upper() for match in _REFERENCE_PATTERN.finditer(text)}
    for found_url in _extract_urls(text):
        tokens.update(_extract_url_ids(found_url))
    if url:
        tokens.update(_extract_url_ids(url))
    return frozenset(sorted(tokens)[:_MAX_REFERENCE_TOKENS])


class GmailAccountScopeDowngradeConflict(RuntimeError):
    """Raised by downgrade() when collapsing account-scoped identity back
    to the pre-GMAIL-002 schema (8634f4be953a) would violate that
    schema's account_key-less UNIQUE constraints. Same check as
    847b7f5c87d8/813c9d5086d0/e6ccb9b4271b's own
    `GmailAccountScopeDowngradeConflict` — duplicated here deliberately
    (not imported), for the same reasons those classes' docstrings give.
    """


def _preflight_check_downgrade_is_safe(connection: sa.engine.Connection) -> None:
    """Identical read-only compatibility check to this chain's earlier
    migrations' own `_preflight_check_downgrade_is_safe` — see this
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
    op.create_table(
        "job_reference_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "token", name="uq_job_reference_tokens_job_token"),
    )
    op.create_index(
        op.f("ix_job_reference_tokens_job_id"), "job_reference_tokens", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_job_reference_tokens_token"), "job_reference_tokens", ["token"], unique=False
    )

    # Backfill: derive tokens for every existing JobRecord using this
    # migration's own self-contained `_extract_reference_tokens` (Round 3,
    # Blocker R3-003) — never the runtime `app.services.email_matching`
    # module, see this file's module docstring and the block comment above
    # `_extract_reference_tokens`.
    connection = op.get_bind()
    existing_jobs = connection.execute(sa.text("SELECT id, title, url FROM jobs")).fetchall()
    now = datetime.now(UTC)
    rows_to_insert = [
        {"job_id": job_id, "token": token, "created_at": now}
        for job_id, title, url in existing_jobs
        for token in _extract_reference_tokens(title or "", url or "")
    ]
    if rows_to_insert:
        job_reference_tokens_table = sa.table(
            "job_reference_tokens",
            sa.column("job_id", sa.Integer),
            sa.column("token", sa.String),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        connection.execute(sa.insert(job_reference_tokens_table), rows_to_insert)


def downgrade() -> None:
    """Downgrade schema.

    Runs the same account-scope compatibility preflight as this chain's
    earlier migrations' own downgrade() — BEFORE this migration's own DDL
    — so a downgrade starting from current HEAD fails closed before
    anything (including this migration's own table drop) has mutated the
    database. See `_preflight_check_downgrade_is_safe` and
    `GmailAccountScopeDowngradeConflict` above, and this module's own
    docstring, for why this check is duplicated here rather than shared.
    """
    _preflight_check_downgrade_is_safe(op.get_bind())

    op.drop_index(op.f("ix_job_reference_tokens_token"), table_name="job_reference_tokens")
    op.drop_index(op.f("ix_job_reference_tokens_job_id"), table_name="job_reference_tokens")
    op.drop_table("job_reference_tokens")
