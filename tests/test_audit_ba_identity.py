"""Audit DB-01: a Bundesagentur vacancy is one row, identified by its referenznummer."""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import JobRecord
from app.db.repositories import update_job_status, upsert_job
from app.models.application_status import ApplicationStatus
from app.models.job import Job, JobScore

REF = "10000-1234567890-S"
BA_URL = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{REF}"
EXTERNAL_URL = "https://karriere.beispiel.example/stellen/4711"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'ba_identity.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _job(url=BA_URL, ref=REF, title="Backend Entwickler (m/w/d)", company="Muster GmbH", **kw):
    return Job(
        source="bundesagentur",
        title=title,
        company=company,
        location="Berlin",
        url=url,
        description=kw.pop("description", ""),
        source_reference=ref,
        **kw,
    )


def _other(url):
    return Job(source="stepstone", title="Dev", company="C", url=url, source_reference="R1")


def _score(value=70):
    return JobScore(score=value, recommendation="MAYBE", data_confidence=0.8)


def _count(db, ref=REF):
    return db.scalar(
        select(func.count()).select_from(JobRecord).where(JobRecord.source_reference == ref)
    )


def test_a_changed_url_for_the_same_reference_updates_the_existing_row(db):
    first, created1 = upsert_job(db, _job(), _score())
    update_job_status(db, first.id, ApplicationStatus.APPLIED)
    second, created2 = upsert_job(db, _job(url=EXTERNAL_URL), _score(80))
    assert created1 is True and created2 is False
    assert second.id == first.id and _count(db) == 1
    db.refresh(second)
    assert second.status == "APPLIED"  # the operator's decision is not reset to NEW
    assert second.score == 80


def test_the_url_flapping_back_and_forth_never_creates_a_double(db):
    for url in (BA_URL, EXTERNAL_URL, BA_URL, EXTERNAL_URL):
        upsert_job(db, _job(url=url), _score())
    assert _count(db) == 1


def test_two_real_vacancies_of_one_employer_stay_two_rows(db):
    upsert_job(db, _job(ref="10000-1-S", url=BA_URL + "a"), _score())
    upsert_job(db, _job(ref="10000-2-S", url=BA_URL + "b"), _score())
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 2


def test_a_posting_without_a_reference_still_uses_the_fingerprint(db):
    first, _ = upsert_job(db, _job(ref=None), _score())
    again, created = upsert_job(db, _job(ref=None), _score())
    other_url, created_other = upsert_job(db, _job(ref=None, url=EXTERNAL_URL), _score())
    assert created is False and again.id == first.id
    assert created_other is True and other_url.id != first.id


def test_existing_duplicates_resolve_to_the_row_the_operator_acted_on(db):
    older, _ = upsert_job(db, _job(), _score())
    newer = JobRecord(
        fingerprint="manual-duplicate",
        source="bundesagentur",
        source_reference=REF,
        title="Backend Entwickler (m/w/d)",
        company="Muster GmbH",
        location="Berlin",
        url=EXTERNAL_URL,
        description="",
        score=50,
        recommendation="MAYBE",
    )
    db.add(newer)
    db.commit()
    update_job_status(db, newer.id, ApplicationStatus.APPLIED)
    outcome, created = upsert_job(db, _job(url="https://x.example/other"), _score(90))
    assert created is False and outcome.id == newer.id
    db.refresh(older)
    assert older.score == 70 and older.status == "NEW"  # untouched


def test_existing_duplicates_without_operator_action_resolve_to_the_oldest(db):
    older, _ = upsert_job(db, _job(), _score())
    db.add(
        JobRecord(
            fingerprint="manual-duplicate",
            source="bundesagentur",
            source_reference=REF,
            title="Backend Entwickler (m/w/d)",
            company="Muster GmbH",
            location="Berlin",
            url=EXTERNAL_URL,
            description="",
            score=50,
            recommendation="MAYBE",
        )
    )
    db.commit()
    outcome, created = upsert_job(db, _job(url="https://x.example/other"), _score(90))
    assert created is False and outcome.id == older.id


def test_other_sources_are_not_affected(db):
    a, _ = upsert_job(
        db,
        Job(
            source="stepstone",
            title="Dev",
            company="C",
            url="https://a.example/1",
            source_reference="R1",
        ),
        _score(),
    )
    b, created = upsert_job(
        db,
        Job(
            source="stepstone",
            title="Dev",
            company="C",
            url="https://a.example/2",
            source_reference="R1",
        ),
        _score(),
    )
    assert created is True and a.id != b.id


def test_a_changed_title_for_the_same_reference_is_not_merged(db):
    senior, _ = upsert_job(db, _job(title="Senior Python Developer"), _score(20))
    junior, created = upsert_job(db, _job(title="Junior Python Developer"), _score(90))
    assert created is True and junior.id != senior.id
    db.refresh(senior)
    assert senior.title == "Senior Python Developer" and senior.score == 20  # not rewritten


def test_a_changed_company_for_the_same_reference_is_not_merged(db):
    first, _ = upsert_job(db, _job(company="Muster GmbH"), _score())
    second, created = upsert_job(db, _job(company="Andere AG"), _score())
    assert created is True and second.id != first.id


def test_title_and_company_comparison_ignores_case_and_surrounding_blanks(db):
    first, _ = upsert_job(db, _job(), _score())
    again, created = upsert_job(
        db,
        _job(url=EXTERNAL_URL, title="  BACKEND entwickler (m/w/d) ", company="muster gmbh"),
        _score(),
    )
    assert created is False and again.id == first.id
