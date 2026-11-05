"""Review 2.2: XING postings sharing company/title/location must not overwrite
each other, and records the operator already acted on are never rewritten."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import JobRecord
from app.db.repositories import descriptions_conflict, upsert_job, upsert_job_observed
from app.models.job import Job, JobScore
from app.services.collector_runner import score_and_persist

SKILLS = ["python"]


def _job(description: str = "", *, source: str = "xing", url: str = "https://www.xing.com/m/aaa"):
    return Job(
        source=source,
        title="Sachbearbeiter (m/w/d)",
        company="Stadtwerke Beispiel",
        location="Leipzig",
        url=url,
        description=description,
    )


def _score(value: int = 40, rec: str = "MAYBE") -> JobScore:
    return JobScore(score=value, recommendation=rec, data_confidence=0.5)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'collision.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _count(db) -> int:
    return db.scalar(select(func.count()).select_from(JobRecord))


class TestDescriptionsConflict:
    @pytest.mark.parametrize(
        ("stored", "incoming", "expected"),
        [
            ("Gehalt: 50.000", "Gehalt: 60.000", True),
            ("Gehalt: 50.000", "  gehalt:   50.000 ", False),  # case/whitespace only
            ("Gehalt: 50.000", "\uff27ehalt: 50.000", False),  # NFKC equivalent
            ("", "Gehalt: 50.000", False),  # blank stored never conflicts
            ("Gehalt: 50.000", "", False),  # empty scrape never conflicts
            ("Gehalt: 50.000", "   ", False),
            (None, "x", False),
        ],
    )
    def test_cases(self, stored, incoming, expected):
        assert descriptions_conflict(stored, incoming) is expected


class TestXingFreeze:
    def test_different_details_keep_the_first_posting_and_flag(self, factory):
        with factory() as db:
            first, created = upsert_job(db, _job("Gehalt: 50.000"), _score(80, "APPLY"))
            assert created
            outcome = upsert_job_observed(
                db, _job("Gehalt: 90.000", url="https://www.xing.com/m/bbb"), _score(10, "SKIP")
            )
            assert outcome.frozen and outcome.possible_collision and not outcome.created
            assert _count(db) == 1
            row = db.get(JobRecord, first.id)
            assert (row.score, row.recommendation, row.description) == (
                80,
                "APPLY",
                "Gehalt: 50.000",
            )
            assert row.url == "https://www.xing.com/m/aaa"

    def test_last_seen_is_still_refreshed(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job("Gehalt: 50.000"), _score())
            before = first.last_seen_at
            upsert_job_observed(db, _job("Gehalt: 90.000"), _score())
            assert db.get(JobRecord, first.id).last_seen_at >= before

    def test_repeat_runs_never_grow_the_table(self, factory):
        with factory() as db:
            upsert_job(db, _job("Gehalt: 50.000"), _score())
            for _ in range(3):
                upsert_job_observed(db, _job("Gehalt: 90.000"), _score())
            assert _count(db) == 1

    def test_blank_incoming_description_is_a_normal_update(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job("Gehalt: 50.000"), _score(40))
            outcome = upsert_job_observed(db, _job(""), _score(70, "APPLY"))
            assert not outcome.frozen and not outcome.possible_collision
            row = db.get(JobRecord, first.id)
            assert (row.score, row.description) == (70, "Gehalt: 50.000")

    def test_identical_description_is_a_normal_update(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job("Gehalt: 50.000"), _score(40))
            outcome = upsert_job_observed(db, _job("gehalt: 50.000"), _score(70, "APPLY"))
            assert not outcome.frozen
            assert db.get(JobRecord, first.id).score == 70

    def test_operator_touched_record_is_not_rewritten(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job("Gehalt: 50.000"), _score(80, "APPLY"))
            first.status = "APPLIED"
            db.commit()
            outcome = upsert_job_observed(db, _job("Gehalt: 50.000"), _score(5, "SKIP"))
            assert outcome.frozen and not outcome.possible_collision
            row = db.get(JobRecord, first.id)
            assert (row.score, row.recommendation, row.status) == (80, "APPLY", "APPLIED")

    def test_other_sources_keep_the_original_overwrite_semantics(self, factory):
        with factory() as db:
            first, _ = upsert_job(
                db, _job("old", source="bundesagentur", url="https://ba/1"), _score(40)
            )
            first.status = "APPLIED"
            db.commit()
            outcome = upsert_job_observed(
                db, _job("new", source="bundesagentur", url="https://ba/1"), _score(70, "APPLY")
            )
            assert not outcome.frozen
            row = db.get(JobRecord, first.id)
            assert (row.score, row.description) == (70, "new")

    def test_historical_insert_only_contract_wins_over_freezing(self, factory):
        with factory() as db:
            upsert_job(db, _job("Gehalt: 50.000"), _score())
            now = datetime.now(UTC)
            with pytest.raises(ValueError, match="historical"):
                upsert_job_observed(
                    db, _job("Gehalt: 90.000"), _score(), first_seen_at=now, last_seen_at=now
                )

    def test_stale_identity_map_copy_cannot_hide_a_concurrent_change(self, factory):
        """Another session fills the description after this session loaded the
        row; the decision must be taken on the reloaded (locked) row."""
        with factory() as db, factory() as other:
            first, _ = upsert_job(db, _job(""), _score())
            held = db.get(JobRecord, first.id)  # strong ref, stays cached
            assert held.description == ""
            row = other.get(JobRecord, first.id)
            row.description = "Gehalt: 50.000"
            other.commit()
            outcome = upsert_job_observed(db, _job("Gehalt: 90.000"), _score())
            assert outcome.frozen and outcome.possible_collision

    def test_race_recovery_winner_honours_the_freeze(self, factory, monkeypatch):
        """Both writers saw no row; the loser's INSERT hits the unique
        fingerprint and must converge through the same freeze rule."""
        from app.db import repositories

        with factory() as winner_db, factory() as db:
            real_get = repositories.get_job_by_fingerprint
            calls = {"n": 0}

            def racing_get(session, job):
                calls["n"] += 1
                if calls["n"] == 1:
                    # the rival commits its row after our first (empty) read
                    upsert_job(winner_db, _job("Gehalt: 50.000"), _score(80, "APPLY"))
                    return None
                return real_get(session, job)

            monkeypatch.setattr(repositories, "get_job_by_fingerprint", racing_get)
            outcome = upsert_job_observed(db, _job("Gehalt: 90.000"), _score(10, "SKIP"))
            assert outcome.frozen and outcome.possible_collision and not outcome.created
            assert _count(db) == 1
            row = db.execute(select(JobRecord)).scalar_one()
            assert (row.score, row.description) == (80, "Gehalt: 50.000")


class TestScoreAndPersistFrozen:
    def test_frozen_result_reports_persisted_values_and_skips_reconciliation(
        self, factory, monkeypatch
    ):
        from app.services import collector_runner

        reconcile_calls = []
        monkeypatch.setattr(
            collector_runner,
            "update_job_score_if_posting_type_unchanged",
            lambda *a, **k: reconcile_calls.append(1) or True,
        )
        with factory() as db:
            record, result, created = score_and_persist(db, SKILLS, _job("Gehalt: 50.000"))
            assert created
            stored_score, stored_rec = record.score, record.recommendation
            record2, result2, created2 = score_and_persist(db, SKILLS, _job("Gehalt: 90.000"))
            assert record2.id == record.id and not created2
            assert result2.possible_collision and result2.is_duplicate
            assert (result2.score, result2.recommendation) == (stored_score, stored_rec)
            assert result2.matched_skills == []
            assert reconcile_calls == []
            assert not db.dirty
