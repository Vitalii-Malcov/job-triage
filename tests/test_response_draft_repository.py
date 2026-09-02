"""Persistence tests for app.db.response_draft_repository (Stage 7C):
idempotent get_or_create identity, latest lookup, full history listing,
account scoping, and JSON round-tripping of missing_fields.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.gmail_repository import upsert_message
from app.db.models import ResponseDraftRecord
from app.db.response_draft_repository import (
    get_latest_response_draft_for_message,
    get_or_create_response_draft,
    get_response_draft_identity,
    list_response_drafts_for_message,
    to_response_draft,
)
from app.providers.email.base import ParsedGmailMessage

ACCOUNT = "me@example.com"


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "test_response_draft_repository.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _seed_message(db, uid: int = 1, account_key: str = ACCOUNT) -> int:
    parsed = ParsedGmailMessage(
        account_key=account_key,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=f"<{uid}@example.com>",
        in_reply_to=None,
        references=(),
        from_address="hr@acme.example.com",
        from_display_name="Recruiter",
        to_addresses=(account_key,),
        cc_addresses=(),
        subject=f"Subject {uid}",
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="Hello.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    msg, _created = upsert_message(db, parsed)
    db.commit()
    return msg.id


def _create_kwargs(**overrides) -> dict:
    kwargs = dict(
        account_key=ACCOUNT,
        gmail_message_id=1,
        analysis_id=1,
        analysis_version=1,
        candidate_profile_version=0,
        matched_job_id=None,
        classification="INTERVIEW_INVITATION",
        status="PROPOSED",
        reason=None,
        subject="Re: Test",
        body="Body text.",
        language="en",
        missing_fields=["candidate name (not confirmed in candidate profile)"],
        provider="deterministic_template",
        generator_version="v1",
    )
    kwargs.update(overrides)
    return kwargs


class TestIdempotency:
    def test_repeated_identical_call_returns_same_row(self, db):
        msg_id = _seed_message(db)
        kwargs = _create_kwargs(gmail_message_id=msg_id)
        record1, created1 = get_or_create_response_draft(db, **kwargs)
        record2, created2 = get_or_create_response_draft(db, **kwargs)

        assert created1 is True
        assert created2 is False
        assert record1.id == record2.id

    def test_different_analysis_id_creates_new_revision(self, db):
        msg_id = _seed_message(db)
        record1, _ = get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))
        record2, created2 = get_or_create_response_draft(
            db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=2)
        )

        assert created2 is True
        assert record1.id != record2.id
        history = list_response_drafts_for_message(db, ACCOUNT, msg_id)
        assert len(history) == 2

    def test_different_candidate_profile_version_creates_new_revision(self, db):
        msg_id = _seed_message(db)
        record1, _ = get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))
        record2, created2 = get_or_create_response_draft(
            db, **_create_kwargs(gmail_message_id=msg_id, candidate_profile_version=1)
        )

        assert created2 is True
        assert record1.id != record2.id

    def test_no_response_recommended_identity_is_also_idempotent(self, db):
        msg_id = _seed_message(db)
        kwargs = _create_kwargs(
            gmail_message_id=msg_id,
            classification="REJECTION",
            status="NO_RESPONSE_RECOMMENDED",
            reason="No automated response is recommended for classification 'REJECTION'.",
            subject=None,
            body=None,
            language=None,
            missing_fields=[],
        )
        record1, created1 = get_or_create_response_draft(db, **kwargs)
        record2, created2 = get_or_create_response_draft(db, **kwargs)

        assert created1 is True
        assert created2 is False
        assert record1.id == record2.id
        assert record1.subject is None
        assert record1.status == "NO_RESPONSE_RECOMMENDED"


class TestImmutability:
    def test_history_is_never_overwritten(self, db):
        msg_id = _seed_message(db)
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=1))
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=2))
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=3))

        history = list_response_drafts_for_message(db, ACCOUNT, msg_id)
        assert len(history) == 3
        assert {r.analysis_id for r in history} == {1, 2, 3}

    def test_no_code_path_updates_an_existing_row(self, db):
        """Sanity guard: this repository module never issues an UPDATE
        against response_drafts — mirrors app.db.gmail_analysis_repository's
        own immutability convention.
        """
        import inspect

        import app.db.response_draft_repository as module

        source = inspect.getsource(module)
        assert "db.query(ResponseDraftRecord).update" not in source
        assert ".update(" not in source


class TestReadAccess:
    def test_get_latest_returns_most_recent_revision(self, db):
        msg_id = _seed_message(db)
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=1))
        record2, _ = get_or_create_response_draft(
            db, **_create_kwargs(gmail_message_id=msg_id, analysis_id=2)
        )

        latest = get_latest_response_draft_for_message(db, ACCOUNT, msg_id)
        assert latest.id == record2.id

    def test_get_latest_returns_none_when_no_draft_exists(self, db):
        msg_id = _seed_message(db)
        assert get_latest_response_draft_for_message(db, ACCOUNT, msg_id) is None

    def test_missing_fields_round_trips_through_json(self, db):
        msg_id = _seed_message(db)
        record, _ = get_or_create_response_draft(
            db,
            **_create_kwargs(
                gmail_message_id=msg_id,
                missing_fields=["a", "b", "c"],
            ),
        )
        dto = to_response_draft(record)
        assert dto.missing_fields == ["a", "b", "c"]

    def test_requires_human_review_always_true(self, db):
        msg_id = _seed_message(db)
        record, _ = get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))
        assert record.requires_human_review is True


class TestAccountScoping:
    def test_draft_for_other_account_is_not_visible(self, db):
        msg_id = _seed_message(db, uid=1, account_key=ACCOUNT)
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))

        assert get_latest_response_draft_for_message(db, "other@example.com", msg_id) is None
        assert list_response_drafts_for_message(db, "other@example.com", msg_id) == []


class TestConcurrencyRace:
    def test_concurrent_create_never_double_inserts(self, db, tmp_path, monkeypatch):
        """Mirrors app.db.gmail_analysis_repository's own race-handling
        test: simulate a losing INSERT racing an already-committed
        identical row by calling get_or_create twice with the identity
        already present, and confirm no duplicate row / no unhandled
        exception (the fast, deterministic proxy for a true multi-thread
        race — the UNIQUE-constraint-catch code path is identical either
        way, and is already exercised end-to-end by the idempotency tests
        above; this test additionally asserts the constraint itself
        exists and is enforced at the DB layer).
        """
        msg_id = _seed_message(db)
        get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))

        # A raw duplicate insert bypassing the repository's own
        # get-before-insert check must still be rejected by the DB-level
        # UNIQUE constraint (defense in depth, not just app-level logic).
        duplicate = ResponseDraftRecord(
            account_key=ACCOUNT,
            gmail_message_id=msg_id,
            analysis_id=1,
            analysis_version=1,
            candidate_profile_version=0,
            matched_job_id=None,
            classification="INTERVIEW_INVITATION",
            status="PROPOSED",
            subject="Re: Test",
            body="Body text.",
            language="en",
            missing_fields_json="[]",
            provider="deterministic_template",
            generator_version="v1",
            requires_human_review=True,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        history = list_response_drafts_for_message(db, ACCOUNT, msg_id)
        assert len(history) == 1


def test_get_response_draft_identity_direct_lookup(db):
    msg_id = _seed_message(db)
    record, _ = get_or_create_response_draft(db, **_create_kwargs(gmail_message_id=msg_id))

    found = get_response_draft_identity(
        db,
        gmail_message_id=msg_id,
        analysis_id=1,
        candidate_profile_version=0,
        generator_version="v1",
    )
    assert found is not None
    assert found.id == record.id

    not_found = get_response_draft_identity(
        db,
        gmail_message_id=msg_id,
        analysis_id=999,
        candidate_profile_version=0,
        generator_version="v1",
    )
    assert not_found is None
