"""Gmail IMAP 32-bit identifiers: every persisted Gmail message UID and
mailbox UIDVALIDITY must hold the full RFC 3501 nz-number range
1..4294967295 (past signed INTEGER's 2147483647), and the inbox parser must
accept exactly that range -- nothing it hands to persistence may overflow.

SQLite stores any INTEGER as 64-bit, so these tests prove the declared
types, the parser bounds, migration parity, the downgrade guard and the
repository identity semantics at the maximum values;
tests/integration/test_gmail_imap_32bit_identifiers_postgres.py proves the
real PostgreSQL overflow before e8b1c4f7a2d9 and its absence after.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import BigInteger, create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

import app.providers.email.imap as gmail_imap_module
from app.db.base import Base
from app.db.gmail_repository import (
    get_known_uids,
    get_message_by_identity,
    record_permanent_skips,
    upsert_message,
)
from app.db.models import (
    GMAIL_MAILBOX_IDENTITY_MAX_LENGTH,
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.providers.email.base import IMAP_NZ_NUMBER_MAX, ParsedGmailMessage
from app.providers.email.draft_base import UID_MAX as DRAFT_UID_MAX
from tests.test_providers_email_imap import ACCOUNT, FakeImapClient, _build_email, _provider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "e8b1c4f7a2d9_gmail_imap_uid_uidvalidity_bigint.py"
)
PRE_FIX_REVISION = "d2f5a8c1b7e4"
# (table, column) for every column widened by e8b1c4f7a2d9.
COLUMNS = (
    ("gmail_messages", "uid_validity"),
    ("gmail_messages", "uid"),
    ("gmail_message_id_claims", "claimant_uid_validity"),
    ("gmail_message_id_claims", "claimant_uid"),
    ("gmail_permanent_skips", "uid_validity"),
    ("gmail_permanent_skips", "uid"),
)
TABLES = ("gmail_messages", "gmail_message_id_claims", "gmail_permanent_skips")
INTEGER_MAX = 2147483647
HIGH = INTEGER_MAX + 1
MAX = 4294967295
ACCOUNT_A = "a@example.com"
ACCOUNT_B = "b@example.com"
SENT_RU = "[Gmail]/Отправленные"


def _load_migration():
    spec = importlib.util.spec_from_file_location("imap_32bit_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the range and the declared types ------------------------------------------------------


def test_one_named_range_constant():
    assert IMAP_NZ_NUMBER_MAX == 2**32 - 1 == MAX
    assert DRAFT_UID_MAX == IMAP_NZ_NUMBER_MAX  # Stage 9D's own bound agrees


def test_every_persisted_imap_uid_and_uidvalidity_is_bigint():
    columns = {
        f"{table.name}.{column.name}": column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if "uid" in column.name and "uuid" not in column.name
    }
    assert set(columns) == {
        *(f"{table}.{column}" for table, column in COLUMNS),
        "gmail_application_drafts.uid_validity",
        "gmail_application_drafts.draft_uid",
        "xing_scan_progress.uid_validity",
        "xing_scan_progress.confirmed_upto_uid",
    }
    for name, column in columns.items():
        assert isinstance(column.type, BigInteger), name


def test_migration_matches_models():
    migration = _load_migration()
    assert migration.revision == "e8b1c4f7a2d9"
    assert migration.down_revision == PRE_FIX_REVISION
    assert migration._INTEGER_MAX == INTEGER_MAX
    assert {(t, c) for t, cols in migration._COLUMNS.items() for c in cols} == set(COLUMNS)


def test_strings_embedding_uid_and_uidvalidity_fit_their_columns():
    # The parser now caps both values at 10 decimal digits.
    assert len(str(IMAP_NZ_NUMBER_MAX)) == 10
    identity = "x" * GMAIL_MAILBOX_IDENTITY_MAX_LENGTH
    suffix = f"{IMAP_NZ_NUMBER_MAX}:{IMAP_NZ_NUMBER_MAX}"
    holder = f"gmail_sync:{identity}:{suffix}:{'0' * 32}"
    assert len(holder) == 666
    assert len(holder) <= GmailThreadRecord.__table__.c.lock_holder.type.length
    thread_key_width = GmailThreadRecord.__table__.c.thread_key.type.length
    for prefix in ("synthetic", "synthetic-collision", "synthetic-ambiguous-reply"):
        assert len(f"{prefix}:{identity}:{suffix}") <= thread_key_width


# --- parser bounds ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (b"1", 1),
        (b"9", 9),
        (b"10", 10),
        (b"2147483647", INTEGER_MAX),
        (b"2147483648", HIGH),
        (b"4294967295", MAX),
        (b"0", None),
        (b"00", None),
        (b"01", None),
        (b"0001", None),
        (b"00000000000000000001", None),
        (b"04294967295", None),
        (b"-1", None),
        (b"+1", None),
        (b"+5", None),
        (b" 1", None),
        (b"1 ", None),
        (b"1_000", None),
        (b"0x10", None),
        (b"abc", None),
        (b"", None),
        ("٥".encode(), None),
        (b"4294967296", None),
        (b"9999999999", None),
        (b"10000000000", None),
        (b"18446744073709551616", None),
        (b"9" * 40, None),
    ],
)
def test_uid_parser_accepts_exactly_the_nz_number_range(token, expected):
    assert gmail_imap_module._parse_imap_uid(token) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"1", 1),
        (b"2147483648", HIGH),
        (b"4294967295", MAX),
        (b"4294967296", None),
        (b"18446744073709551616", None),
    ],
)
def test_status_uidvalidity_range_is_unchanged(value, expected):
    data = [b'"INBOX" (UIDVALIDITY ' + value + b")"]
    assert gmail_imap_module._parse_status_uid_validity(data) == expected


class _RawSearchClient(FakeImapClient):
    """UID SEARCH returns these exact tokens; FETCH must never be called
    for a token the parser rejects."""

    def __init__(self, tokens: list[bytes], **kwargs) -> None:
        super().__init__(**kwargs)
        self._tokens = tokens

    def uid(self, command: str, *args):
        if command == "search":
            self.uid_calls.append((command, args))
            return ("OK", [b" ".join(self._tokens)])
        return super().uid(command, *args)


@pytest.mark.asyncio
async def test_invalid_uid_tokens_are_skipped_before_any_fetch():
    invalid = [b"0", b"-1", b"+5", b"1_000", b"abc", b"4294967296", b"9" * 30]
    raw = _build_email(plaintext_body="max uid")
    client = _RawSearchClient([*invalid, b"4294967295"], messages={MAX: raw}, uid_validity=MAX)
    known_lookups = []

    def get_known_uids_spy(uid_validity, candidate_uids):
        known_lookups.append((uid_validity, list(candidate_uids)))
        return set()

    result = await _provider(client, get_known_uids=get_known_uids_spy).fetch()

    fetched = {args[0] for command, args in client.uid_calls if command == "fetch"}
    assert fetched == {b"4294967295"}
    assert known_lookups == [(MAX, [MAX])]
    assert [(m.uid_validity, m.uid) for m in result.messages] == [(MAX, MAX)]
    assert result.skipped_count == len(invalid)
    assert result.permanently_skipped == ()


def _fetches(client) -> list[tuple]:
    return [args for command, args in client.uid_calls if command == "fetch"]


@pytest.mark.asyncio
async def test_uid32_001_invalid_prefix_does_not_consume_the_sync_cap():
    """Codex UID32-001: 500 (= MAX_MESSAGES_PER_SYNC) malformed tokens ahead
    of a valid UID used to fill the cap, so the valid message was never
    fetched -- on every sync, until it aged out of the lookback window."""
    assert gmail_imap_module.MAX_MESSAGES_PER_SYNC == 500
    malformed = [b"abc", b"0", b"0001", b"+5", b"4294967296"] * 100
    raw = _build_email(plaintext_body="behind the malformed prefix")
    client = _RawSearchClient([*malformed, b"4294967295"], messages={MAX: raw})

    result = await _provider(client).fetch()

    assert [m.uid for m in result.messages] == [MAX]
    assert {args[0] for args in _fetches(client)} == {b"4294967295"}
    assert result.skipped_count == len(malformed)  # one per invalid token, no cap overflow
    assert result.permanently_skipped == ()


@pytest.mark.asyncio
async def test_uid32_001_repeated_malformed_search_still_makes_forward_progress(db):
    """Through the real sync service and repository: the valid message is
    persisted on the first run, the second identical SEARCH finds it known,
    and no malformed token is ever fetched or recorded as a permanent skip."""
    from app.services.gmail_inbox import GmailInboxService

    malformed = [b"abc", b"0", b"0001", b"+5", b"4294967296"] * 100
    tokens = [*malformed, b"4294967295"]
    raw = _build_email(plaintext_body="behind the malformed prefix")
    results, clients = [], []
    for _ in range(2):
        client = _RawSearchClient(tokens, messages={MAX: raw}, uid_validity=MAX)
        provider = _provider(
            client,
            get_known_uids=lambda v, uids: get_known_uids(db, ACCOUNT, "INBOX", v, uids),
        )
        results.append(await GmailInboxService().sync(db, provider))
        clients.append(client)

    first, second = results
    assert (first.fetched, first.created, first.skipped, first.failed) == (1, 1, 500, 0)
    assert (second.fetched, second.created, second.skipped) == (0, 0, 500)
    assert [args[0] for args in _fetches(clients[0])] == [b"4294967295", b"4294967295"]
    assert _fetches(clients[1]) == []  # already known: nothing re-fetched
    stored = db.scalars(select(GmailMessageRecord)).all()
    assert [(r.uid_validity, r.uid) for r in stored] == [(MAX, MAX)]
    assert db.scalars(select(GmailPermanentSkipRecord)).all() == []


@pytest.mark.asyncio
async def test_uid32_001_filter_dedup_and_cap_order_with_mixed_tokens(monkeypatch):
    """invalid + valid + known + duplicate + high UIDs under a cap of 3:
    invalid tokens are dropped first, then known ones, duplicates collapse,
    and only then does the cap take the oldest (lowest) remaining UIDs."""
    monkeypatch.setattr(gmail_imap_module, "MAX_MESSAGES_PER_SYNC", 3)
    tokens = [b"abc", b"7", b"0005", b"3", b"3", b"4294967295", b"9", b"1", b"-2", b"7"]
    messages = {uid: _build_email(plaintext_body=f"uid {uid}") for uid in (1, 3, 7, 9, MAX)}
    client = _RawSearchClient(tokens, messages=messages)
    lookups = []

    def known(uid_validity, uids):
        lookups.append(list(uids))
        return {3}

    result = await _provider(client, get_known_uids=known).fetch()

    assert lookups == [[7, 3, MAX, 9, 1]]  # valid, deduplicated, SEARCH order
    assert [m.uid for m in result.messages] == [1, 7, 9]  # oldest 3 of {7, MAX, 9, 1}
    assert [args[0] for args in _fetches(client)] == [
        b"1",
        b"1",
        b"7",
        b"7",
        b"9",
        b"9",  # size probe + body per UID
    ]
    # 3 invalid tokens (abc, 0005, -2) + 1 capped valid UID (MAX); the known
    # UID 3 and the duplicate tokens are neither fetched nor skipped.
    assert result.skipped_count == 4
    assert result.permanently_skipped == ()


@pytest.mark.asyncio
async def test_uid32_002_leading_zero_uid_is_rejected_before_any_fetch(db):
    from app.services.gmail_inbox import GmailInboxService

    client = _RawSearchClient([b"0001"], messages={1: _build_email(plaintext_body="uid 1")})
    lookups = []

    def known(uid_validity, uids):
        lookups.append(list(uids))
        return set()

    result = await GmailInboxService().sync(db, _provider(client, get_known_uids=known))

    assert _fetches(client) == []  # neither the RFC822.SIZE probe nor BODY.PEEK[]
    assert lookups == []
    assert (result.fetched, result.created, result.skipped) == (0, 0, 1)
    assert db.scalars(select(GmailMessageRecord)).all() == []
    assert db.scalars(select(GmailPermanentSkipRecord)).all() == []


# --- repository semantics at the maximum values ------------------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'imap_32bit.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _parsed(*, account_key=ACCOUNT_A, mailbox="INBOX", uid_validity=MAX, uid=MAX, **kw):
    fields = {"message_id_header": None, "in_reply_to": None, "references": ()}
    fields.update(kw)
    return ParsedGmailMessage(
        account_key=account_key,
        mailbox=mailbox,
        uid=uid,
        uid_validity=uid_validity,
        from_address="recruiter@example.com",
        from_display_name=None,
        to_addresses=("me@example.com",),
        cc_addresses=(),
        subject="Hello",
        sent_at=None,
        direction="INBOUND",
        body_plain="hello",
        body_truncated=False,
        has_html=False,
        attachments=(),
        **fields,
    )


def test_same_identity_at_max_values_is_deduplicated(db):
    first, created_first = upsert_message(db, _parsed(message_id_header="<max@example.com>"))
    second, created_second = upsert_message(db, _parsed(message_id_header="<max@example.com>"))

    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    stored = db.scalars(select(GmailMessageRecord)).all()
    assert [(r.uid_validity, r.uid) for r in stored] == [(MAX, MAX)]
    assert get_message_by_identity(db, ACCOUNT_A, "INBOX", MAX, MAX).id == first.id


@pytest.mark.parametrize(
    "difference",
    [{"uid": MAX - 1}, {"uid_validity": MAX - 1}, {"account_key": ACCOUNT_B}],
    ids=["different-uid", "different-uidvalidity", "different-account"],
)
def test_different_identity_at_max_values_stays_independent(db, difference):
    first, _ = upsert_message(db, _parsed())
    second, created = upsert_message(db, _parsed(**difference))

    assert created is True
    assert first.id != second.id
    assert len(db.scalars(select(GmailMessageRecord)).all()) == 2


def test_permanent_skips_round_trip_max_values(db):
    record_permanent_skips(db, ACCOUNT_A, SENT_RU, MAX, [(MAX, "OVERSIZED")])
    record_permanent_skips(db, ACCOUNT_A, SENT_RU, MAX, [(MAX, "OVERSIZED")])

    rows = db.scalars(select(GmailPermanentSkipRecord)).all()
    assert [(r.uid_validity, r.uid) for r in rows] == [(MAX, MAX)]
    assert get_known_uids(db, ACCOUNT_A, SENT_RU, MAX, [MAX, MAX - 1]) == {MAX}
    assert get_known_uids(db, ACCOUNT_A, SENT_RU, MAX - 1, [MAX]) == set()
    assert get_known_uids(db, ACCOUNT_B, SENT_RU, MAX, [MAX]) == set()


def test_message_id_claim_keeps_max_values_without_false_contest(db):
    root, _ = upsert_message(db, _parsed(message_id_header="<root@example.com>"))
    # Re-import of the SAME provider identity must not contest its own claim.
    upsert_message(db, _parsed(message_id_header="<root@example.com>"))
    reply, _ = upsert_message(
        db,
        _parsed(
            uid=MAX - 1,
            message_id_header="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references=("<root@example.com>",),
        ),
    )

    claims = {c.message_id_header: c for c in db.scalars(select(GmailMessageIdClaimRecord))}
    claim = claims["<root@example.com>"]
    assert (claim.claimant_uid_validity, claim.claimant_uid) == (MAX, MAX)
    assert claim.contested is False
    assert claim.thread_id == root.thread_id
    assert reply.thread_id == root.thread_id


@pytest.mark.parametrize(
    "difference", [{"uid": MAX - 1}, {"uid_validity": MAX - 1}], ids=["uid", "uidvalidity"]
)
def test_same_message_id_from_other_provider_identity_is_a_real_contest(db, difference):
    # Control: a DIFFERENT provider identity claiming the same Message-ID is
    # still detected -- the max-value compare is not vacuous.
    first, _ = upsert_message(db, _parsed(message_id_header="<dup@example.com>"))
    other, _ = upsert_message(db, _parsed(message_id_header="<dup@example.com>", **difference))

    claim = db.scalars(select(GmailMessageIdClaimRecord)).one()
    assert (claim.claimant_uid_validity, claim.claimant_uid) == (MAX, MAX)
    assert claim.contested is True
    assert other.thread_id != first.thread_id


# --- migration (SQLite: structure and downgrade guard) -----------------------------------


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _shape(engine):
    inspector = inspect(engine)
    return {
        table: (
            {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)},
            sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)),
            sorted(tuple(i["column_names"]) for i in inspector.get_indexes(table)),
            sorted(c["sqltext"] for c in inspector.get_check_constraints(table)),
        )
        for table in TABLES
    }


def test_upgrade_changes_only_the_six_types(tmp_path):
    db_path = tmp_path / "imap_32bit_upgrade.db"
    cfg = _alembic_config(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    upgrade(cfg, PRE_FIX_REVISION)
    before = _shape(engine)
    upgrade(cfg, "head")
    after = _shape(engine)

    widened = {table: {c for t, c in COLUMNS if t == table} for table in TABLES}
    for table in TABLES:
        cols_before, *rest_before = before[table]
        cols_after, *rest_after = after[table]
        for column in widened[table]:
            assert cols_before[column] == ("INTEGER", False)
            assert cols_after[column] == ("BIGINT", False)
        assert {k: v for k, v in cols_after.items() if k not in widened[table]} == {
            k: v for k, v in cols_before.items() if k not in widened[table]
        }
        assert rest_after == rest_before
        # both `> 0` CHECKs survive the batch rebuild
        assert sum(check.endswith("uid > 0") for check in rest_after[2]) == 1
        assert sum(check.endswith("uid_validity > 0") for check in rest_after[2]) == 1


def _insert_skip(engine, uid_validity: int, uid: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO gmail_permanent_skips "
                "(account_key, mailbox, uid_validity, uid, reason, created_at) "
                "VALUES ('a@example.com', 'INBOX', :v, :u, 'OVERSIZED', CURRENT_TIMESTAMP)"
            ),
            {"v": uid_validity, "u": uid},
        )


@pytest.mark.parametrize(
    ("uid_validity", "uid", "column"),
    [(MAX, 1, "uid_validity"), (1, MAX, "uid")],
    ids=["uidvalidity-too-large", "uid-too-large"],
)
def test_downgrade_refuses_values_above_integer_max(tmp_path, uid_validity, uid, column):
    db_path = tmp_path / "imap_32bit_guard.db"
    cfg = _alembic_config(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    upgrade(cfg, "head")
    _insert_skip(engine, uid_validity, uid)

    with pytest.raises(RuntimeError, match="Refusing to narrow Gmail IMAP") as excinfo:
        downgrade(cfg, PRE_FIX_REVISION)
    message = str(excinfo.value)
    assert str(MAX) not in message
    assert f"'gmail_permanent_skips.{column}': 1" in message

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            "e8b1c4f7a2d9"
        )
        row = connection.execute(text("SELECT uid_validity, uid FROM gmail_permanent_skips")).one()
        assert tuple(row) == (uid_validity, uid)
    for table in TABLES:  # nothing was narrowed before the refusal
        cols = _shape(engine)[table][0]
        assert all(cols[c] == ("BIGINT", False) for t, c in COLUMNS if t == table)


def test_downgrade_then_upgrade_cycle_keeps_in_range_values(tmp_path):
    db_path = tmp_path / "imap_32bit_cycle.db"
    cfg = _alembic_config(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    upgrade(cfg, "head")
    _insert_skip(engine, INTEGER_MAX, INTEGER_MAX)

    downgrade(cfg, PRE_FIX_REVISION)
    assert _shape(engine)["gmail_permanent_skips"][0]["uid"] == ("INTEGER", False)
    upgrade(cfg, "head")

    assert _shape(engine)["gmail_permanent_skips"][0]["uid"] == ("BIGINT", False)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT uid_validity, uid FROM gmail_permanent_skips")).one()
        assert tuple(row) == (INTEGER_MAX, INTEGER_MAX)
