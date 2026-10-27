"""Stage 17: real-PostgreSQL proof of the candidate search configuration
(migration a9d3f6b2c4e8):

- the configuration is applied through the same atomic compare-and-swap as
  on SQLite and records one revision;
- `candidate_profile_revisions` is append-only in the database (Core
  UPDATE/DELETE refused by the trigger);
- `job_commute_checks` rows follow their job (ON DELETE CASCADE).

**Local execution:** skipped unless `TEST_POSTGRES_URL` is set; run
`alembic upgrade head` against that database first (no create_all).
**CI:** part of the `scheduler-postgres` job's zero-skip gate, at head.
"""

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy.exc
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.candidate_search_repository import (
    apply_config_plan,
    load_search_config,
    plan_config_change,
)
from app.db.job_commute_repository import latest_commute_check, record_commute_check
from app.db.models import CandidateProfileRevisionRecord, JobCommuteCheckRecord, JobRecord
from app.models.candidate_search import CandidateConfigSpec

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

_TRUNCATE = text(
    "TRUNCATE candidate_profile_revisions, job_commute_checks, candidate_profiles "
    "RESTART IDENTITY CASCADE"
)
SPEC = CandidateConfigSpec.model_validate(
    {
        "schema": "jobtriage.candidate-config/1",
        "source": "USER_CONFIRMED",
        "confirmed_on": "2026-10-27",
        "profile": {"location_city": "Musterstadt"},
        "search": {
            "home_city": "Musterstadt",
            "commute_origin_label": "Musterstadt Bahnhof",
            "max_commute_km": 100,
            "commute_measure": "CAR_ROUTE",
            "remote_work_scope": "GERMANY",
            "remote_work_from_region": "Hessen",
        },
    }
)


@pytest.fixture()
def factory():
    engine = create_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    with engine.begin() as conn:
        # Revision DELETEs are refused by the append-only trigger; test
        # isolation uses the privileged TRUNCATE instead.
        conn.execute(_TRUNCATE)
    yield sessionmaker(engine)
    with engine.begin() as conn:
        conn.execute(_TRUNCATE)
        conn.execute(text("DELETE FROM jobs WHERE fingerprint LIKE 'stage17-%'"))
    engine.dispose()


def test_configuration_is_applied_with_one_revision(factory):
    session = factory()
    try:
        profile = get_or_create_candidate_profile(session)
        version = apply_config_plan(
            session, plan_config_change(profile, SPEC), actor="ci", reason="postgres"
        )
        session.expire_all()
        config = load_search_config(session)
        assert version == profile.profile_version
        assert (config.location_city, config.search.max_commute_km) == ("Musterstadt", 100)
        assert session.scalar(select(func.count()).select_from(CandidateProfileRevisionRecord)) == 1
        assert plan_config_change(session.get(type(profile), 1), SPEC).is_noop
    finally:
        session.close()


def test_revisions_are_append_only_in_postgres(factory):
    session = factory()
    try:
        profile = get_or_create_candidate_profile(session)
        apply_config_plan(session, plan_config_change(profile, SPEC), actor="ci", reason="pg")
    finally:
        session.close()
    engine = factory.kw["bind"]
    for statement in (
        update(CandidateProfileRevisionRecord).values(reason="rewritten"),
        delete(CandidateProfileRevisionRecord),
    ):
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(statement)


def test_commute_checks_follow_their_job(factory):
    session = factory()
    try:
        job = JobRecord(
            fingerprint="stage17-commute",
            source="bundesagentur",
            title="Python Developer",
            company="Example GmbH",
            location="Darmstadt",
            url="https://example.org/job",
            score=50,
            recommendation="MAYBE",
        )
        session.add(job)
        session.commit()
        record_commute_check(
            session,
            job_id=job.id,
            origin_label="Musterstadt Bahnhof",
            destination_label="Darmstadt",
            car_route_km=41.2,
            method="MANUAL",
            source_note="ci",
            now=datetime(2026, 10, 27, tzinfo=UTC),
        )
        assert latest_commute_check(session, job.id).car_route_km == 41.2
        session.delete(job)
        session.commit()
        assert session.scalar(select(func.count()).select_from(JobCommuteCheckRecord)) == 0
    finally:
        session.close()
