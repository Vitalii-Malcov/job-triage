"""Stage 15: real-PostgreSQL proof of the application lifecycle tables
(migration b5d2e8f4a1c7):

- CHECK constraints and UNIQUE(correlation_key) are enforced by PostgreSQL;
- the same inbound message processed concurrently from many threads yields
  exactly ONE event and ONE transition (UNIQUE correlation key + CAS);
- two DIFFERENT pieces of evidence racing on one application never both
  win (CAS on status+version) and the status always equals the last
  status event;
- a one-step downgrade removes both tables and the upgrade restores them.

SQLite's single writer serializes these trivially; MVCC is where a
check-then-act would race. No IMAP/SMTP/Telegram anywhere.

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first (no create_all).
**CI:** part of the `scheduler-postgres` job's zero-skip gate; runs after the
at-head modules (its last test cycles one migration and restores head).
"""

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_correlation import InboundMessage
from app.services.application_response_processor import ApplicationResponseProcessor
from app.services.application_tracker import (
    ApplicationConcurrencyError,
    ApplicationTracker,
)

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVISION = "b5d2e8f4a1c7"
PREVIOUS = "a7c3e9f1b2d4"
SUBMITTED_AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)


@pytest.fixture()
def factory():
    engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM application_events"))
        conn.execute(text("DELETE FROM applications"))
    yield sessionmaker(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM application_events"))
        conn.execute(text("DELETE FROM applications"))
    engine.dispose()


def _create(session, ref="20385-2347902-S"):
    return (
        ApplicationTracker()
        .create_application(
            session,
            company_name="ALD Vacuum Technologies GmbH",
            role_title="Backend Developer (m/w/div.)",
            source="bundesagentur",
            source_reference=ref,
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=S.SUBMITTED,
            origin=O.IMPORT,
            actor="pg-test",
            occurred_at=SUBMITTED_AT,
            employer_domains=("ald-vt.de",),
            correlation_key=f"portal-submission:{ref}",
        )
        .application
    )


def _receipt() -> InboundMessage:
    return InboundMessage(
        account_key="applicant@example.invalid",
        message_key="mid:pg-receipt@ald-vt.de",
        message_id_header="<pg-receipt@ald-vt.de>",
        reply_to_ids=(),
        from_address="karriere@ald-vt.de",
        from_display_name="ALD Vacuum Technologies GmbH",
        to_addresses=("applicant@example.invalid",),
        subject="Ihre Bewerbung - Eingangsbestätigung",
        body="Zusendung Ihrer Bewerbung, deren Eingang wir hiermit bestätigen.",
        has_html=False,
        occurred_at=SUBMITTED_AT + timedelta(minutes=3),
    )


def _race(count, fn):
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(index):
        barrier.wait()
        try:
            results[index] = fn(index)
        except Exception as exc:  # collected for assertions
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def _counts(session, app_id):
    events = session.execute(
        text("SELECT COUNT(*) FROM application_events WHERE application_id = :a"), {"a": app_id}
    ).scalar_one()
    status = session.execute(
        text("SELECT current_status FROM applications WHERE id = :a"), {"a": app_id}
    ).scalar_one()
    return events, status


class TestApplicationLifecycleOnPostgres:
    def test_constraints_are_enforced(self, factory):
        session = factory()
        app = _create(session)
        for statement in (
            "UPDATE applications SET current_status = 'APPLIED' WHERE id = :a",
            "UPDATE applications SET application_channel = 'FAX' WHERE id = :a",
            "INSERT INTO application_events (application_id, sequence, event_type, old_status, "
            "new_status, origin, actor, recorded_at, metadata_json) VALUES (:a, 9, "
            "'STATUS_OVERRIDDEN', 'SUBMITTED', 'REJECTED', 'OPERATOR', 'x', now(), '{}')",
            "INSERT INTO application_events (application_id, sequence, event_type, origin, actor, "
            "recorded_at, metadata_json, correlation_key) VALUES (:a, 9, 'INBOUND_MESSAGE', "
            "'GMAIL', 'x', now(), '{}', 'portal-submission:20385-2347902-S')",
        ):
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                session.execute(text(statement), {"a": app.id})
                session.commit()
            session.rollback()
        assert _create(session).id == app.id  # idempotent create
        assert _counts(session, app.id) == (1, "SUBMITTED")
        session.close()

    def test_same_message_processed_concurrently_is_applied_once(self, factory):
        setup = factory()
        app = _create(setup)
        setup.close()

        def process(_index):
            session = factory()
            try:
                return ApplicationResponseProcessor().process(session, [_receipt()], apply=True)[0]
            finally:
                session.close()

        results = _race(8, process)
        applied = [r for r in results if not isinstance(r, Exception) and r.transition_applied]
        assert len(applied) == 1
        for result in results:
            assert isinstance(result, Exception) is False
            assert (
                result.transition_applied
                or result.duplicate
                or (result.reason == "CONCURRENT_CHANGE_RETRY")
            )
        check = factory()
        assert _counts(check, app.id) == (2, "RECEIVED")
        check.close()

    def test_competing_evidence_never_both_wins(self, factory):
        setup = factory()
        app = _create(setup)
        setup.close()
        targets = [S.RECEIVED, S.REJECTED, S.INTERVIEW, S.WITHDRAWN]

        def transition(index):
            session = factory()
            try:
                return ApplicationTracker().transition_application(
                    session,
                    app.id,
                    targets[index],
                    origin=O.GMAIL,
                    actor="pg-race",
                    correlation_key=f"gmail:race-{index}",
                )
            finally:
                session.close()

        results = _race(len(targets), transition)
        winners = [r for r in results if not isinstance(r, Exception) and r.transition_applied]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) >= 1
        assert all(
            isinstance(e, ApplicationConcurrencyError | sqlalchemy.exc.IntegrityError)
            or type(e).__name__ == "InvalidApplicationTransitionError"
            for e in losers
        )
        check = factory()
        events, status = _counts(check, app.id)
        assert events == 1 + len(winners)
        last = check.execute(
            text(
                "SELECT new_status FROM application_events WHERE application_id = :a "
                "ORDER BY sequence DESC LIMIT 1"
            ),
            {"a": app.id},
        ).scalar_one()
        assert status == last
        sequences = (
            check.execute(
                text(
                    "SELECT sequence FROM application_events WHERE application_id = :a ORDER BY 1"
                ),
                {"a": app.id},
            )
            .scalars()
            .all()
        )
        assert sequences == list(range(1, events + 1))
        check.close()

    def test_one_step_downgrade_and_upgrade(self, factory):
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", TEST_POSTGRES_URL.replace("%", "%%"))
        engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        try:
            downgrade(cfg, PREVIOUS)
            names = set(inspect(engine).get_table_names())
            assert not {"applications", "application_events"} & names
            upgrade(cfg, "head")
            inspector = inspect(engine)
            assert {"applications", "application_events"} <= set(inspector.get_table_names())
            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == (REVISION)
        finally:
            upgrade(cfg, "head")
            engine.dispose()
