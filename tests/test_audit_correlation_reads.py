"""Audit PERF-02 (part 2): repeated candidate scans and per-application event reads are shared
without changing what the response processor decides."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services import application_response_processor as processor
from app.services.application_response_processor import ApplicationResponseProcessor
from tests import stage15_fixtures as fx
from tests.test_stage15_response_tracking import _app, _msg

ALD_SENDER_ONLY = "Wir laden Sie gerne zu einem Vorstellungsgespräch ein."


class _UncachedReads(processor._ReadCache):
    """The behaviour before PERF-02: every read goes to the database."""

    def candidates(self, db):
        return list(self._tracker.iter_correlation_candidates(db, only_ids=self._only_ids))

    def events(self, db, application_id):
        return list(processor.repo.list_events(db, application_id))


def _database(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    _app(session)  # ALD
    _app(
        session,
        company="Beta Systems GmbH",
        role="Data Engineer",
        ref="B-1",
        domains=("beta.example",),
    )
    _app(
        session,
        company="Beta Systems GmbH",
        role="Python Developer",
        ref="B-2",
        domains=("beta.example",),
    )
    session.commit()
    return engine, session


def _messages():
    later = fx.ALD_SUBMITTED_AT + timedelta(days=1)
    return [
        _msg(),  # ALD receipt: matched
        _msg(),  # same key again: duplicate
        _msg(
            subject="Einladung",
            body=ALD_SENDER_ONLY,
            sender="hr@beta.example",
            display="Beta Systems HR",
            key="mid:beta-1@beta.example",
            message_id="<beta-1@beta.example>",
            at=later,
        ),  # company only: two candidate applications
        _msg(
            subject="Newsletter",
            body="Unser Newsletter",
            sender="news@elsewhere.example",
            display="Elsewhere",
            key="mid:news@elsewhere.example",
            message_id="<news@elsewhere.example>",
            at=later,
        ),  # unrelated
    ]


def _run(session, *, apply):
    return [
        r.as_dict()
        for r in ApplicationResponseProcessor().process(
            session, _messages(), apply=apply, applicant_addresses=("applicant@example.invalid",)
        )
    ]


@pytest.mark.parametrize("apply", [False, True])
def test_results_equal_uncached_reads(tmp_path, monkeypatch, apply):
    engine_new, session_new = _database(tmp_path, "new.db")
    new = _run(session_new, apply=apply)

    monkeypatch.setattr(processor, "_ReadCache", _UncachedReads)
    engine_old, session_old = _database(tmp_path, "old.db")
    old = _run(session_old, apply=apply)

    assert new == old
    assert {r["correlation_status"] for r in new} >= {"MATCHED"}
    session_new.close()
    session_old.close()
    engine_new.dispose()
    engine_old.dispose()


def test_a_dry_run_scans_candidates_once_per_batch(tmp_path, monkeypatch):
    engine, session = _database(tmp_path, "scan.db")
    scans = {"n": 0}
    real = processor.ApplicationTracker.iter_correlation_candidates

    def counting(self, db, **kwargs):
        scans["n"] += 1
        return real(self, db, **kwargs)

    monkeypatch.setattr(processor.ApplicationTracker, "iter_correlation_candidates", counting)
    _run(session, apply=False)
    assert scans["n"] == 1  # was 5: once per message, twice for an unmatched-from-sender one

    scans["n"] = 0
    _run(session, apply=True)
    assert scans["n"] <= len(_messages())  # applying never shares a scan across messages
    session.close()
    engine.dispose()


def test_events_are_read_once_per_application_in_a_dry_run(tmp_path, monkeypatch):
    engine, session = _database(tmp_path, "events.db")
    reads = []
    real = processor.repo.list_events

    def counting(db, application_id):
        reads.append(application_id)
        return real(db, application_id)

    monkeypatch.setattr(processor.repo, "list_events", counting)
    _run(session, apply=False)
    assert len(reads) == len(set(reads))
    session.close()
    engine.dispose()
