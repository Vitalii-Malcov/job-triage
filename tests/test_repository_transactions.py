"""Architecture 3: transaction boundaries of two real multi-step operations.

1. `score_and_persist` = deferred upsert + posting-type reconciliation, ONE commit.
2. Company-research update = content/version update + alias claim, ONE commit.
"""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import repositories
from app.db.base import Base
from app.db.models import (
    CompanyResearchIdentityAlias,
    CompanyResearchRecord,
    JobRecord,
    JobReferenceTokenRecord,
)
from app.db.repositories import (
    record_failed_attempt,
    update_job_score_if_posting_type_unchanged,
    upsert_company_research,
    upsert_job,
    upsert_job_observed,
)
from app.models.company_research import CompanyResearchData, Evidence
from app.models.job import Job, JobScore
from app.services import collector_runner as runner
from app.services.collector_runner import JobScoreReconciliationError, score_and_persist

SKILLS = ["python", "fastapi", "sqlalchemy"]


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tx.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _job(posting_type=None, title="Backend Engineer") -> Job:
    return Job(
        source="bundesagentur",
        title=title,
        company="TxCo",
        location="Berlin",
        url="https://example.com/jobs/tx-1",
        posting_type=posting_type,
        must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
    )


def _row(factory):
    with factory() as fresh:
        return fresh.execute(
            select(JobRecord.posting_type, JobRecord.recommendation, JobRecord.score)
        ).first()


def _count(factory, model) -> int:
    with factory() as fresh:
        return fresh.scalar(select(func.count()).select_from(model))


class TestDeferredUpsert:
    def test_deferred_upsert_is_not_durable_until_the_caller_commits(self, factory):
        with factory() as db:
            outcome = upsert_job_observed(
                db, _job(), JobScore(score=50, recommendation="MAYBE"), commit=False
            )
            assert outcome.created
            assert _count(factory, JobRecord) == 0  # a fresh session sees nothing yet
            db.rollback()
        assert _count(factory, JobRecord) == 0
        assert _count(factory, JobReferenceTokenRecord) == 0

    def test_deferred_upsert_commits_row_and_tokens_together(self, factory):
        with factory() as db:
            upsert_job_observed(
                db, _job(), JobScore(score=50, recommendation="MAYBE"), commit=False
            )
            db.commit()
        assert _count(factory, JobRecord) == 1

    def test_deferred_mode_refuses_a_session_with_pending_changes(self, factory):
        with factory() as db:
            db.add(
                JobRecord(
                    fingerprint="x",
                    source="s",
                    title="t",
                    company="c",
                    location="l",
                    url="u",
                    description="",
                    score=1,
                    recommendation="SKIP",
                    status="NEW",
                )
            )
            with pytest.raises(ValueError, match="pending changes"):
                upsert_job_observed(
                    db, _job(), JobScore(score=50, recommendation="MAYBE"), commit=False
                )

    def test_cas_in_deferred_mode_does_not_commit(self, factory):
        with factory() as db:
            record, _ = upsert_job(db, _job(), JobScore(score=50, recommendation="MAYBE"))
            applied = update_job_score_if_posting_type_unchanged(
                db,
                record.id,
                observed_posting_type=None,
                score=5,
                recommendation="SKIP",
                data_confidence=0.5,
                commit=False,
            )
            assert applied
            db.rollback()
        assert _row(factory).score == 50  # the deferred CAS vanished with the rollback

    def test_race_recovery_in_deferred_mode_converges_on_the_winner(self, factory, monkeypatch):
        with factory() as winner_db, factory() as db:
            real_get = repositories.get_job_by_fingerprint
            calls = {"n": 0}

            def racing_get(session, job):
                calls["n"] += 1
                if calls["n"] == 1:
                    upsert_job(winner_db, _job(), JobScore(score=80, recommendation="APPLY"))
                    return None
                return real_get(session, job)

            monkeypatch.setattr(repositories, "get_job_by_fingerprint", racing_get)
            outcome = upsert_job_observed(
                db, _job(), JobScore(score=60, recommendation="MAYBE"), commit=False
            )
            assert not outcome.created
            db.commit()
        assert _count(factory, JobRecord) == 1


class TestScoreAndPersistIsOneTransaction:
    def test_failure_before_reconciliation_leaves_the_previous_consistent_row(
        self, factory, monkeypatch
    ):
        from app.db.candidate_profile_repository import get_candidate_skills_for_scoring  # noqa: F401

        with factory() as db:
            # a typed (excluded) row is already persisted by "call B"
            score_and_persist(db, SKILLS, _job(posting_type="SELBSTAENDIGKEIT"))
        before = tuple(_row(factory))

        real_get = runner.get_job_by_fingerprint
        calls = {"n": 0}

        def stale_existing(db_, job_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(db_, job_)

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure before the reconciliation CAS")

        monkeypatch.setattr(runner, "get_job_by_fingerprint", stale_existing)
        monkeypatch.setattr(runner, "update_job_score_if_posting_type_unchanged", boom)
        with factory() as db, pytest.raises(RuntimeError, match="injected"):
            # untyped call A: scored APPLY against a stale 'untyped' observation
            score_and_persist(db, SKILLS, _job(posting_type=None))
        assert tuple(_row(factory)) == before  # never the inconsistent (typed, APPLY) pair

    def test_reconciliation_exhaustion_leaves_none_of_the_operation_durable(
        self, factory, monkeypatch
    ):
        with factory() as db:
            score_and_persist(db, SKILLS, _job(posting_type="SELBSTAENDIGKEIT"))
        before = tuple(_row(factory))

        real_get = runner.get_job_by_fingerprint
        calls = {"n": 0}

        def stale_existing(db_, job_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(db_, job_)

        monkeypatch.setattr(runner, "get_job_by_fingerprint", stale_existing)
        monkeypatch.setattr(
            runner, "update_job_score_if_posting_type_unchanged", lambda *a, **k: False
        )
        with factory() as db, pytest.raises(JobScoreReconciliationError):
            score_and_persist(db, SKILLS, _job(posting_type=None))
        assert tuple(_row(factory)) == before

    def test_success_publishes_one_consistent_state_and_a_clean_session(self, factory, monkeypatch):
        with factory() as db:
            score_and_persist(db, SKILLS, _job(posting_type="SELBSTAENDIGKEIT"))
        real_get = runner.get_job_by_fingerprint
        calls = {"n": 0}

        def stale_existing(db_, job_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(db_, job_)

        monkeypatch.setattr(runner, "get_job_by_fingerprint", stale_existing)
        with factory() as db:
            record, result, created = score_and_persist(db, SKILLS, _job(posting_type=None))
            assert not created and result.is_duplicate
            assert not db.dirty and not db.new
            db.commit()  # a later unrelated commit must not replay stale scoring
        posting_type, recommendation, _ = tuple(_row(factory))
        assert posting_type == "SELBSTAENDIGKEIT" and recommendation != "APPLY"

    def test_plain_first_write_is_durable_after_the_call(self, factory):
        with factory() as db:
            score_and_persist(db, SKILLS, _job())
        assert _count(factory, JobRecord) == 1 and _count(factory, JobReferenceTokenRecord) >= 0

    def test_frozen_xing_observation_still_commits_its_last_seen_refresh(self, factory):
        """A row the operator already acted on is only touched (last_seen_at), and that single
        write is committed with the call."""
        xing = Job(
            source="xing",
            title="Sachbearbeiter",
            company="Co",
            location="Leipzig",
            url="https://www.xing.com/m/a",
            description="Gehalt: 50.000",
        )
        with factory() as db:
            first, _, _ = score_and_persist(db, SKILLS, xing)
            first.status = "APPLIED"
            db.commit()
            seen = first.last_seen_at
            _, result, created = score_and_persist(db, SKILLS, xing)
            assert result.is_duplicate and not created and not result.possible_collision
        with factory() as fresh:
            row = fresh.execute(select(JobRecord)).scalar_one()
            assert row.status == "APPLIED" and row.last_seen_at >= seen

    def test_a_xing_sibling_is_one_transaction_and_leaves_the_first_posting_alone(self, factory):
        xing = Job(
            source="xing",
            title="Sachbearbeiter",
            company="Co",
            location="Leipzig",
            url="https://www.xing.com/m/a",
            description="Gehalt: 50.000",
        )
        rival = xing.model_copy(update={"description": "Gehalt: 90.000"})
        with factory() as db:
            score_and_persist(db, SKILLS, xing)
            _, result, created = score_and_persist(db, SKILLS, rival)
            assert created and result.sibling_posting and not result.possible_collision
        with factory() as fresh:
            rows = fresh.execute(select(JobRecord).order_by(JobRecord.id)).scalars().all()
            assert [r.description for r in rows] == ["Gehalt: 50.000", "Gehalt: 90.000"]


def _data(**overrides) -> CompanyResearchData:
    fields = {
        "company_name": "Acme GmbH",
        "provider_name": "job_data",
        "research_status": "PARTIAL",
        "evidence": [Evidence(type="FACT", claim="test claim", source_url="https://example.com")],
    }
    fields.update(overrides)
    return CompanyResearchData(**fields)


class TestCompanyResearchAliasIsAtomicWithTheUpdate:
    def _seed_failed(self, db):
        return record_failed_attempt(
            db,
            normalized_domain=None,
            normalized_company_name="acme gmbh",
            company_name="Acme GmbH",
            provider_name="job_data",
            error_message="boom",
        )

    def test_alias_failure_rolls_back_content_and_version(self, factory, monkeypatch):
        with factory() as db:
            failed = self._seed_failed(db)
            failed_id, version = failed.id, failed.version

        def boom(*args, **kwargs):
            raise RuntimeError("injected alias failure")

        monkeypatch.setattr(repositories, "_claim_company_research_alias", boom)
        with factory() as db, pytest.raises(RuntimeError, match="injected alias"):
            upsert_company_research(
                db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
            )
        with factory() as fresh:
            row = fresh.get(CompanyResearchRecord, failed_id)
            assert row.research_status == "FAILED" and row.version == version
            assert fresh.scalar(select(func.count()).select_from(CompanyResearchIdentityAlias)) == 0

    def test_success_claims_the_alias_in_the_same_commit(self, factory):
        with factory() as db:
            failed = self._seed_failed(db)
            record, _ = upsert_company_research(
                db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
            )
            assert record.id == failed.id
        with factory() as fresh:
            alias = fresh.execute(select(CompanyResearchIdentityAlias)).scalar_one()
            assert alias.company_research_id == failed.id

    def test_an_existing_alias_owner_is_never_reassigned(self, factory):
        with factory() as db:
            owner, _ = upsert_company_research(
                db,
                _data(),
                normalized_domain="owner.example",
                normalized_company_name="acme gmbh",
            )
            owner_id = owner.id
            other = self._seed_failed_other(db)
            upsert_company_research(
                db, _data(), normalized_domain="other.example", normalized_company_name="acme gmbh"
            )
        with factory() as fresh:
            alias = fresh.execute(select(CompanyResearchIdentityAlias)).scalar_one()
            assert alias.company_research_id == owner_id
        assert other is not None

    @staticmethod
    def _seed_failed_other(db):
        return record_failed_attempt(
            db,
            normalized_domain="other.example",
            normalized_company_name="acme gmbh",
            company_name="Acme GmbH",
            provider_name="job_data",
            error_message="boom",
        )


class TestReviewRound1:
    @pytest.mark.asyncio
    async def test_repeat_ba_collection_of_a_vacancy_with_a_description_is_an_update(self, factory):
        """The enrichment path used to stage `existing.description`, which made the
        session dirty and tripped the deferred-upsert guard on every repeat run."""
        from app.services.collector_runner import enrich_and_persist_ba_job

        class NoDetail:
            async def fetch_detail(self, reference):
                raise AssertionError("a stored description must be reused, not fetched")

        job = _job().model_copy(update={"description": "Stored description with python"})
        with factory() as db:
            score_and_persist(db, SKILLS, job)
        with factory() as db:
            record, _result, created = await enrich_and_persist_ba_job(
                db, NoDetail(), SKILLS, _job().model_copy(update={"description": ""})
            )
            assert created is False and record.description == "Stored description with python"

    def test_missing_candidate_profile_is_not_created_mid_transaction(self, factory, monkeypatch):
        """A typed reconciliation needs the employment preferences; with no profile
        row yet the lookup must be a pure read (equivalent to an empty profile), not a
        create-and-commit that would publish the deferred upsert early."""
        from app.db.models import CandidateProfileRecord

        with factory() as db:
            score_and_persist(db, SKILLS, _job(posting_type="SELBSTAENDIGKEIT"))
            db.query(CandidateProfileRecord).delete()  # no profile row at all
            db.commit()
        before = tuple(_row(factory))

        real_get = runner.get_job_by_fingerprint
        calls = {"n": 0}

        def stale_existing(db_, job_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(db_, job_)

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure before the reconciliation CAS")

        monkeypatch.setattr(runner, "get_job_by_fingerprint", stale_existing)
        monkeypatch.setattr(runner, "update_job_score_if_posting_type_unchanged", boom)
        with factory() as db, pytest.raises(RuntimeError, match="injected"):
            score_and_persist(
                db, SKILLS, _job(posting_type=None).model_copy(update={"description": "new text"})
            )
        assert tuple(_row(factory)) == before
        with factory() as fresh:
            assert fresh.scalar(select(func.count()).select_from(CandidateProfileRecord)) == 0
            assert fresh.execute(select(JobRecord.description)).scalar_one() != "new text"
