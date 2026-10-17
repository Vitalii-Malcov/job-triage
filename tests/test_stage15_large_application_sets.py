"""Stage 15 robustness: more than one page of applications.

Regression for a review finding: candidate selection for correlation used
`list_applications()` with a default page of 200, so application #201+
could never be correlated. Correlation now scans every submitted
application in bounded keyset batches; listing is explicitly paginated.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.stage15_application_tracker as cli
from app.core.config import Settings
from app.db import application_repository as repo
from app.db.base import Base
from app.db.models import ApplicationRecord
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.security import rate_limit as rate_limit_module
from app.services.application_correlation import InboundMessage, correlate_message
from app.services.application_response_processor import ApplicationResponseProcessor
from app.services.application_tracker import ApplicationTracker
from tests import stage15_fixtures as fx

FILLERS = 205


def _fill(session, count=FILLERS):
    now = datetime.now(UTC)
    session.add_all(
        ApplicationRecord(
            company_name=f"Filler {i} GmbH",
            role_title="Python Developer (m/w/d)",
            source="bundesagentur",
            source_reference=f"FILL-{i:06d}",
            application_channel="EMAIL",
            employer_domains_json=json.dumps([f"filler{i}.example"]),
            current_status="SUBMITTED",
            submitted_at=fx.ALD_SUBMITTED_AT - timedelta(minutes=5),
            last_status_changed_at=now,
            version=1,
            created_at=now,
            updated_at=now,
        )
        for i in range(count)
    )
    session.commit()


def _ald(session, role="Backend Developer (m/w/div.)", ref="20385-2347902-S"):
    return (
        ApplicationTracker()
        .create_application(
            session,
            company_name="ALD Vacuum Technologies GmbH",
            role_title=role,
            source="bundesagentur",
            source_reference=ref,
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=S.SUBMITTED,
            origin=O.IMPORT,
            actor="test",
            occurred_at=fx.ALD_SUBMITTED_AT,
            employer_domains=("ald-vt.de",),
            correlation_key=f"portal-submission:{ref}",
        )
        .application
    )


def _receipt(key="mid:ald-large@ald-vt.de"):
    return InboundMessage(
        account_key="applicant@example.invalid",
        message_key=key,
        message_id_header="<ald-large@ald-vt.de>",
        reply_to_ids=(),
        from_address=fx.ALD_RECEIPT_FROM,
        from_display_name=fx.ALD_RECEIPT_DISPLAY_NAME,
        to_addresses=("applicant@example.invalid",),
        subject=fx.ALD_RECEIPT_SUBJECT,
        body=fx.ALD_RECEIPT_TEXT,
        has_html=False,
        occurred_at=fx.ALD_RECEIPT_ARRIVAL,
    )


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'large.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, autoflush=False)
    engine.dispose()


def test_application_after_the_first_200_is_correlated_and_transitioned(factory):
    db = factory()
    _fill(db)
    ald = _ald(db)
    assert ald.id > 200
    result = ApplicationResponseProcessor().process(db, [_receipt()], apply=True)[0]
    assert result.application_id == ald.id
    assert (result.previous_status, result.new_status) == ("SUBMITTED", "RECEIVED")
    assert result.transition_applied
    replay = ApplicationResponseProcessor().process(db, [_receipt()], apply=True)[0]
    assert replay.duplicate and not replay.transition_applied
    assert len(ApplicationTracker().get_application_history(db, ald.id)) == 2
    db.close()


def test_ambiguity_is_detected_across_batch_boundaries(factory):
    db = factory()
    first = _ald(db, role="Data Engineer (m/w/d)", ref="20385-0000001-S")
    _fill(db)
    second = _ald(db)
    assert first.id == 1 and second.id > 200
    result = ApplicationResponseProcessor().process(db, [_receipt()], apply=True)[0]
    assert result.correlation_status == "AMBIGUOUS" and not result.event_recorded
    db.close()


@pytest.mark.parametrize("batch_size", [1, 7, 200, 500])
def test_candidate_scan_is_complete_ordered_and_batched(factory, batch_size, monkeypatch):
    db = factory()
    _fill(db)
    ald = _ald(db)
    queries = []
    original = db.scalars

    def counting(statement, *args, **kwargs):
        queries.append(statement)
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalars", counting)
    ids = [
        a.id for a in ApplicationTracker().iter_correlation_candidates(db, batch_size=batch_size)
    ]
    assert ids == list(range(1, ald.id + 1))
    assert len(queries) == ald.id // batch_size + 1  # one query per batch, never one huge read
    monkeypatch.undo()
    only = list(ApplicationTracker().iter_correlation_candidates(db, only_ids=frozenset({ald.id})))
    assert [a.id for a in only] == [ald.id]
    db.close()


def test_scan_skips_unsubmitted_and_rejects_unbounded_batches(factory):
    db = factory()
    _fill(db, count=3)
    ApplicationTracker().create_application(
        db,
        company_name="Draft GmbH",
        role_title="Dev",
        source="bundesagentur",
        source_reference="DRAFT-1",
        application_channel=C.OTHER,
        origin=O.SYSTEM,
        actor="t",
    )
    assert [a.id for a in ApplicationTracker().iter_correlation_candidates(db)] == [1, 2, 3]
    for bad in (0, 501):
        with pytest.raises(ValueError):
            list(repo.iter_applications(db, batch_size=bad))
    db.close()


def test_correlation_consumes_a_lazy_iterable(factory):
    db = factory()
    _fill(db)
    ald = _ald(db)
    candidates = ApplicationTracker().iter_correlation_candidates(db, batch_size=50)
    assert correlate_message(_receipt(), candidates).application_id == ald.id
    db.close()


def test_listing_is_explicitly_paginated(factory):
    db = factory()
    _fill(db)
    ald = _ald(db)
    tracker = ApplicationTracker()
    first = tracker.list_applications(db, limit=200)
    assert [a.id for a in first] == list(range(1, 201))
    rest = tracker.list_applications(db, limit=200, after_id=first[-1].id)
    assert rest[-1].id == ald.id and len(rest) == ald.id - 200
    assert tracker.list_applications(db, (S.SUBMITTED,), limit=500)[-1].id == ald.id
    for bad in (0, 501):
        with pytest.raises(ValueError):
            tracker.list_applications(db, limit=bad)
    db.close()


def test_api_pages_beyond_the_first_200(factory, monkeypatch):
    db = factory()
    _fill(db)
    ald = _ald(db)
    db.close()

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override
    settings = Settings(api_key="k", rate_limit_requests=1000, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.security.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: settings)
    rate_limit_module._requests.clear()
    headers = {"X-API-Key": "k"}
    try:
        with TestClient(fastapi_app) as client:
            page = client.get("/api/v1/applications?limit=200", headers=headers).json()
            assert len(page) == 200 and page[-1]["id"] == 200
            nxt = client.get("/api/v1/applications?limit=200&after_id=200", headers=headers)
            assert nxt.json()[-1]["id"] == ald.id
            default = client.get("/api/v1/applications", headers=headers).json()
            assert len(default) == 50
            too_big = client.get("/api/v1/applications?limit=201", headers=headers)
            assert too_big.status_code == 422
    finally:
        fastapi_app.dependency_overrides.clear()
        rate_limit_module._requests.clear()


def test_cli_list_never_truncates_silently(tmp_path, capsys):
    from alembic.command import upgrade
    from alembic.config import Config

    from tests.test_stage15_response_tracking import PROJECT_ROOT

    path = tmp_path / "cli_large.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    _fill(session)
    ald = _ald(session)
    session.close()
    engine.dispose()
    assert cli.run(["--database", str(path), "list"]) == 0
    captured = capsys.readouterr()
    assert "MORE: page is full; continue with --after-id 200" in captured.err
    assert cli.run(["--database", str(path), "list", "--after-id", "200"]) == 0
    captured = capsys.readouterr()
    assert f"id={ald.id} " in captured.out and "MORE" not in captured.err
    assert cli.run(["--database", str(path), "list", "--limit", "501"]) == 2
