"""Codex mailbox-width finding: every Gmail mailbox the config accepts
(at most MAX_MAILBOX_NAME_LENGTH decoded code points) must have a
canonical modified-UTF-7 identity that fits the persisted columns.

SQLite never enforces VARCHAR length, so these tests prove the BOUND
(derived worst case <= declared width, model == migration == constant);
tests/integration/test_gmail_mailbox_width_postgres.py proves the real
PostgreSQL enforcement before and after d2f5a8c1b7e4.
"""

import importlib.util
import math
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.core.config import Settings
from app.db.gmail_repository import new_thread_lock_holder_token
from app.db.models import (
    GMAIL_MAILBOX_IDENTITY_MAX_LENGTH,
    GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH,
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.providers.email.base import MAX_MAILBOX_NAME_LENGTH
from app.providers.email.imap_mailbox import (
    canonical_mailbox_identity,
    decode_modified_utf7,
    mailbox_identity_aliases,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "d2f5a8c1b7e4_widen_gmail_mailbox_identity_columns.py"
)
PRE_FIX_REVISION = "f3b8d2a6c9e1"
# (table, column, pre-fix width, migrated width)
COLUMNS = (
    ("gmail_messages", "mailbox", 100, GMAIL_MAILBOX_IDENTITY_MAX_LENGTH),
    ("gmail_message_id_claims", "claimant_mailbox", 100, GMAIL_MAILBOX_IDENTITY_MAX_LENGTH),
    ("gmail_permanent_skips", "mailbox", 100, GMAIL_MAILBOX_IDENTITY_MAX_LENGTH),
    ("gmail_threads", "lock_holder", 200, GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH),
)
UID_MAX = 4294967295

CYRILLIC_40 = "Я" * 40
# One modified-UTF-7 run of 100 supplementary code points: the worst case.
WORST_CASE = "\U0001f600" * MAX_MAILBOX_NAME_LENGTH


def _worst_case_canonical_length(limit: int) -> int:
    """Exhaustive DP over every shape a decoded name of <= `limit` code
    points can take in modified UTF-7 (printable ASCII = 1, "&" = 2, a run
    of code points = "&" + base64(UTF-16BE) + "-", adjacent runs merge).
    A supplementary code point (2 UTF-16 units) dominates any BMP or
    control character inside a run, so runs are scored as all-supplementary.
    The "already canonical" and unencodable paths return the name itself
    (<= limit), which is always smaller."""
    best_after_ascii = [0] * (limit + 1)  # next token may start a run
    best_after_run = [0] * (limit + 1)  # next token must not be a run
    for n in range(1, limit + 1):
        best_after_run[n] = best_after_ascii[n - 1] + 2  # literal "&" -> "&-"
        run = max(2 + math.ceil(16 * k / 3) + best_after_run[n - k] for k in range(1, n + 1))
        best_after_ascii[n] = max(best_after_run[n], run)
    return best_after_ascii[limit]


def _load_migration():
    spec = importlib.util.spec_from_file_location("mailbox_width_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the bound ----------------------------------------------------------------------


def test_worst_case_canonical_length_is_536_and_fits_the_columns():
    worst = _worst_case_canonical_length(MAX_MAILBOX_NAME_LENGTH)
    assert worst == 536
    assert len(canonical_mailbox_identity(WORST_CASE)) == worst
    assert worst <= GMAIL_MAILBOX_IDENTITY_MAX_LENGTH


@pytest.mark.parametrize(
    "name",
    [
        WORST_CASE,
        "Я" * 100,
        "&" * 100,
        "\U0001f600&" * 50,
        "Я&" * 50,
        "a\U0001f600" * 50,
        "\x7f" * 100,
        "[Gmail]/" + "\U0001f600" * 92,
    ],
    ids=["supp-run", "bmp-run", "ampersands", "supp-amp", "bmp-amp", "mixed", "ctl", "prefix"],
)
def test_near_worst_case_names_are_accepted_and_fit(name):
    settings = Settings(gmail_mailbox=name, gmail_sent_mailbox=name)
    identity = canonical_mailbox_identity(settings.gmail_mailbox)
    assert len(identity) <= _worst_case_canonical_length(MAX_MAILBOX_NAME_LENGTH)
    assert len(identity) <= GMAIL_MAILBOX_IDENTITY_MAX_LENGTH
    assert identity.isascii()
    assert decode_modified_utf7(identity) == name


def test_model_columns_and_migration_share_the_constants():
    migration = _load_migration()
    assert migration.down_revision == PRE_FIX_REVISION
    assert migration._MAILBOX_OLD_LENGTH == MAX_MAILBOX_NAME_LENGTH
    assert {(t, c, old, new) for t, c, _, old, new in migration._COLUMNS} == set(COLUMNS)
    models = {
        "gmail_messages": GmailMessageRecord,
        "gmail_message_id_claims": GmailMessageIdClaimRecord,
        "gmail_permanent_skips": GmailPermanentSkipRecord,
        "gmail_threads": GmailThreadRecord,
    }
    for table, column, nullable, _, new_length in migration._COLUMNS:
        model_column = models[table].__table__.c[column]
        assert model_column.type.length == new_length
        assert model_column.nullable is nullable


def test_strings_embedding_the_identity_fit_their_columns():
    # The longest identity any column can hold, with the widest UID values.
    identity = "x" * GMAIL_MAILBOX_IDENTITY_MAX_LENGTH
    holder = new_thread_lock_holder_token(f"gmail_sync:{identity}:{UID_MAX}:{UID_MAX}")
    assert len(holder) == 666
    assert len(holder) <= GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH
    thread_key_width = GmailThreadRecord.__table__.c.thread_key.type.length
    for prefix in ("synthetic", "synthetic-collision", "synthetic-ambiguous-reply"):
        assert len(f"{prefix}:{identity}:{UID_MAX}:{UID_MAX}") <= thread_key_width


# --- the reproducer and identity semantics ------------------------------------------


def test_forty_cyrillic_characters_exceed_the_old_width():
    settings = Settings(gmail_mailbox=CYRILLIC_40)
    identity = canonical_mailbox_identity(settings.gmail_mailbox)
    assert len(identity) == 109
    assert len(identity) > MAX_MAILBOX_NAME_LENGTH


@pytest.mark.parametrize("readable", [CYRILLIC_40, WORST_CASE, "Jobs & " + "Я" * 90])
def test_readable_and_encoded_spellings_share_one_long_identity(readable):
    encoded = canonical_mailbox_identity(readable)
    assert encoded != readable
    assert canonical_mailbox_identity(encoded) == encoded
    assert set(mailbox_identity_aliases(readable)) == {readable, encoded}
    assert set(mailbox_identity_aliases(encoded)) == {readable, encoded}


@pytest.mark.parametrize(
    ("name", "identity"),
    [
        ("INBOX", "INBOX"),
        ("[Gmail]/Sent Mail", "[Gmail]/Sent Mail"),
        ("[Gmail]/Отправленные", "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"),
    ],
)
def test_ordinary_mailboxes_are_unchanged(name, identity):
    assert canonical_mailbox_identity(name) == identity


# --- the migration (SQLite: structure and downgrade guard) ---------------------------


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _shape(engine):
    inspector = inspect(engine)
    return {
        table: (
            {c["name"]: getattr(c["type"], "length", None) for c in inspector.get_columns(table)},
            sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)),
            sorted(tuple(i["column_names"]) for i in inspector.get_indexes(table)),
        )
        for table, *_ in COLUMNS
    }


def test_upgrade_widens_only_the_identity_columns(tmp_path):
    cfg = _alembic_config(tmp_path / "mailbox_width.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'mailbox_width.db'}")
    upgrade(cfg, PRE_FIX_REVISION)
    before = _shape(engine)
    upgrade(cfg, "head")
    after = _shape(engine)

    for table, column, old_length, new_length in COLUMNS:
        cols_before, uniques_before, indexes_before = before[table]
        cols_after, uniques_after, indexes_after = after[table]
        assert cols_before[column] == old_length
        assert cols_after[column] == new_length
        assert {k: v for k, v in cols_after.items() if k != column} == {
            k: v for k, v in cols_before.items() if k != column
        }
        assert uniques_after == uniques_before
        assert indexes_after == indexes_before


def _insert_skip(engine, mailbox: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO gmail_permanent_skips "
                "(account_key, mailbox, uid_validity, uid, reason, created_at) "
                "VALUES ('a@example.com', :mailbox, 100, 1, 'OVERSIZED', CURRENT_TIMESTAMP)"
            ),
            {"mailbox": mailbox},
        )


def test_downgrade_refuses_to_truncate_long_identities(tmp_path):
    db_path = tmp_path / "mailbox_width_guard.db"
    cfg = _alembic_config(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    upgrade(cfg, "head")
    long_identity = canonical_mailbox_identity(CYRILLIC_40)
    _insert_skip(engine, long_identity)

    with pytest.raises(RuntimeError, match="Refusing to narrow") as excinfo:
        downgrade(cfg, PRE_FIX_REVISION)
    assert long_identity not in str(excinfo.value)
    assert "gmail_permanent_skips.mailbox" in str(excinfo.value)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            "d2f5a8c1b7e4"
        )
        assert connection.execute(text("SELECT mailbox FROM gmail_permanent_skips")).scalar() == (
            long_identity
        )
    assert _shape(engine)["gmail_permanent_skips"][0]["mailbox"] == (
        GMAIL_MAILBOX_IDENTITY_MAX_LENGTH
    )


def test_downgrade_then_upgrade_cycle_keeps_short_identities(tmp_path):
    db_path = tmp_path / "mailbox_width_cycle.db"
    cfg = _alembic_config(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    upgrade(cfg, "head")
    _insert_skip(engine, "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-")

    downgrade(cfg, PRE_FIX_REVISION)
    assert _shape(engine)["gmail_permanent_skips"][0]["mailbox"] == MAX_MAILBOX_NAME_LENGTH
    upgrade(cfg, "head")

    assert _shape(engine)["gmail_permanent_skips"][0]["mailbox"] == (
        GMAIL_MAILBOX_IDENTITY_MAX_LENGTH
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT mailbox FROM gmail_permanent_skips")).scalar() == (
            "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"
        )
