"""XING vacancy identity (TASK 20).

Digest e-mails carry no stable posting id: the per-recipient tracking link is a per-message
position slot. Identity evidence is therefore text only. These tests prove that postings which
share company/title/location but differ in salary/employment text become SEPARATE rows
(distinguished, not hidden), that an existing row is never rewritten by a different posting,
and that ambiguity is reported instead of guessed.
"""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.collectors.xing_email import assign_occurrences
from app.db import repositories
from app.db.base import Base
from app.db.models import JobRecord
from app.db.repositories import (
    _fingerprint,
    _variant_fingerprint,
    descriptions_conflict,
    resolve_xing_identity,
    upsert_job,
    upsert_job_observed,
)
from app.models.job import Job, JobScore
from app.services.collector_runner import score_and_persist

SKILLS = ["python"]
A = "Gehalt: 50.000 € - 60.000 €; Beschäftigung: Vollzeit"
B = "Gehalt: 70.000 € - 80.000 €; Beschäftigung: Vollzeit"
C = "Beschäftigung: Teilzeit"


def _job(
    description: str = "",
    *,
    source: str = "xing",
    url: str = "https://www.xing.com/m/aaa",
    occurrence: int = 1,
    title: str = "Sachbearbeiter (m/w/d)",
):
    return Job(
        source=source,
        title=title,
        company="Stadtwerke Beispiel",
        location="Leipzig",
        url=url,
        description=description,
        occurrence=occurrence,
    )


def _score(value: int = 40, rec: str = "MAYBE") -> JobScore:
    return JobScore(score=value, recommendation=rec, data_confidence=0.5)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'identity.db'}", connect_args={"check_same_thread": False}
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
            ("Gehalt: 50.000", "  gehalt:   50.000 ", False),
            ("Gehalt: 50.000", "Ｇehalt: 50.000", False),
            ("", "Gehalt: 50.000", False),
            ("Gehalt: 50.000", "", False),
            ("Gehalt: 50.000", "   ", False),
            (None, "x", False),
        ],
    )
    def test_cases(self, stored, incoming, expected):
        assert descriptions_conflict(stored, incoming) is expected


class TestDistinguishedPostings:
    def test_different_details_become_two_rows_and_the_first_is_untouched(self, factory):
        with factory() as db:
            first, created = upsert_job(db, _job(A), _score(80, "APPLY"))
            assert created
            outcome = upsert_job_observed(
                db, _job(B, url="https://www.xing.com/m/bbb"), _score(10, "SKIP")
            )
            assert outcome.created and outcome.sibling_created and not outcome.frozen
            assert _count(db) == 2
            row = db.get(JobRecord, first.id)
            assert (row.score, row.recommendation, row.description) == (80, "APPLY", A)
            assert row.url == "https://www.xing.com/m/aaa"
            sibling = outcome.record
            assert sibling.id != first.id
            assert (sibling.score, sibling.description, sibling.status) == (10, B, "NEW")
            assert sibling.identity_key == _fingerprint(_job(A)) == row.identity_key
            assert sibling.fingerprint == _variant_fingerprint(
                sibling.identity_key, repositories._normalized_description(B), 1
            )

    def test_each_posting_is_then_updated_only_in_its_own_row(self, factory):
        with factory() as db:
            a, _ = upsert_job(db, _job(A), _score(40))
            b = upsert_job_observed(db, _job(B), _score(40)).record
            outcome_a = upsert_job_observed(db, _job(A), _score(70, "APPLY"))
            outcome_b = upsert_job_observed(db, _job(B), _score(20, "SKIP"))
            assert outcome_a.record.id == a.id and not outcome_a.created
            assert outcome_b.record.id == b.id and not outcome_b.created
            assert (db.get(JobRecord, a.id).score, db.get(JobRecord, b.id).score) == (70, 20)
            assert _count(db) == 2

    def test_repeat_runs_never_grow_the_table(self, factory):
        with factory() as db:
            for _ in range(4):
                for description in (A, B, C):
                    upsert_job_observed(db, _job(description), _score())
            assert _count(db) == 3

    def test_tracking_link_is_not_part_of_the_identity(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job(A, url="https://www.xing.com/m/one"), _score())
            outcome = upsert_job_observed(db, _job(A, url="https://www.xing.com/m/two"), _score())
            assert outcome.record.id == first.id and not outcome.created
            assert _count(db) == 1

    def test_normalized_evidence_is_the_same_posting(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job("Gehalt: 50.000"), _score())
            outcome = upsert_job_observed(db, _job("  gehalt:   50.000 "), _score(70, "APPLY"))
            assert outcome.record.id == first.id and not outcome.sibling_created
            assert db.get(JobRecord, first.id).score == 70

    def test_full_partial_blank_full_sequence_does_not_multiply_rows(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job(A), _score(40))
            partial = upsert_job_observed(db, _job(C), _score(40))  # partial evidence: sibling
            assert partial.sibling_created
            for description in ("", A, "", C, A):
                upsert_job_observed(db, _job(description), _score(40))
            assert _count(db) == 2
            assert db.get(JobRecord, first.id).description == A


class TestEvidenceFreeObservations:
    def test_blank_in_a_one_row_group_is_a_normal_update(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job(A), _score(40))
            outcome = upsert_job_observed(db, _job(""), _score(70, "APPLY"))
            assert not outcome.frozen and not outcome.possible_collision
            row = db.get(JobRecord, first.id)
            assert (row.score, row.description) == (70, A)

    def test_blank_in_a_multi_row_group_is_ambiguous_and_changes_nothing(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job(A), _score(80, "APPLY"))
            upsert_job_observed(db, _job(B), _score(60, "MAYBE"))
            before = (first.last_seen_at, first.score, first.recommendation, first.url)
            outcome = upsert_job_observed(db, _job(""), _score(5, "SKIP"))
            assert outcome.frozen and outcome.possible_collision and not outcome.created
            assert _count(db) == 2
            db.expire_all()
            row = db.get(JobRecord, first.id)
            assert (row.last_seen_at, row.score, row.recommendation, row.url) == before

    def test_blank_on_an_empty_group_inserts_the_base_row(self, factory):
        with factory() as db:
            outcome = upsert_job_observed(db, _job(""), _score())
            assert outcome.created and not outcome.sibling_created
            assert outcome.record.fingerprint == _fingerprint(_job(""))

    def test_blank_legacy_base_is_never_adopted_by_later_evidence(self, factory):
        """A row stored without evidence stays as it is; weaker-evidence rewriting would
        risk a row that already has applications attached."""
        with factory() as db:
            legacy, _ = upsert_job(db, _job(""), _score(40))
            legacy.identity_key = None
            db.commit()
            outcome = upsert_job_observed(db, _job(A), _score(90, "APPLY"))
            assert outcome.created and outcome.sibling_created
            row = db.get(JobRecord, legacy.id)
            assert (row.description, row.score) == ("", 40)


class TestOccurrence:
    def test_second_identical_block_with_another_link_is_its_own_row_and_replay_is_stable(
        self, factory
    ):
        with factory() as db:
            first = upsert_job_observed(db, _job(A, occurrence=1), _score()).record
            second = upsert_job_observed(db, _job(A, occurrence=2), _score())
            assert second.created and second.sibling_created
            assert second.record.id != first.id
            again = upsert_job_observed(db, _job(A, occurrence=2), _score())
            assert again.record.id == second.record.id and not again.created
            assert _count(db) == 2

    def test_blank_evidence_ignores_the_occurrence(self, factory):
        """A block without salary/type text carries no identity evidence: identical blank
        blocks collapse (documented limit) and never pick another occurrence's row."""
        with factory() as db:
            first = upsert_job_observed(db, _job("", occurrence=1), _score()).record
            second = upsert_job_observed(db, _job("", occurrence=2), _score())
            assert second.record.id == first.id and not second.created and _count(db) == 1

    def test_blank_occurrence_two_never_creates_a_row_in_a_multi_row_group(self, factory):
        with factory() as db:
            upsert_job(db, _job(A), _score())
            upsert_job_observed(db, _job(B), _score())
            outcome = upsert_job_observed(db, _job("", occurrence=2), _score())
            assert outcome.possible_collision and not outcome.created
            assert _count(db) == 2

    def test_partial_failure_replay_keeps_both_occurrences(self, factory):
        """Occurrence 2 was persisted, occurrence 1 failed; the replay must produce the
        second row instead of folding occurrence 1 into occurrence 2's row."""
        with factory() as db:
            two = upsert_job_observed(db, _job(A, occurrence=2), _score()).record
            one = upsert_job_observed(db, _job(A, occurrence=1), _score())
            assert one.created and one.record.id != two.id
            assert _count(db) == 2
            replay_two = upsert_job_observed(db, _job(A, occurrence=2), _score())
            replay_one = upsert_job_observed(db, _job(A, occurrence=1), _score())
            assert replay_two.record.id == two.id and replay_one.record.id == one.record.id
            assert _count(db) == 2

    def test_assign_occurrences_drops_a_repeated_block_with_the_same_link(self):
        one = _job(A, url="https://www.xing.com/m/x1")
        repeat = _job(A, url="https://www.xing.com/m/x1")
        assert [j.occurrence for j in assign_occurrences([one, repeat])] == [1]

    def test_assign_occurrences_blank_evidence_stays_occurrence_one(self):
        one = _job("", url="https://www.xing.com/m/x1")
        two = _job("", url="https://www.xing.com/m/x2")
        assert [j.occurrence for j in assign_occurrences([one, two])] == [1, 1]

    def test_assign_occurrences_uses_the_persistence_group_semantics(self):
        """Company names that the repository treats as different groups must not be counted
        together here: occurrence and group always agree."""
        one = _job(A, url="https://www.xing.com/m/x1").model_copy(update={"company": "A  B"})
        two = _job(A, url="https://www.xing.com/m/x2").model_copy(update={"company": "A B"})
        assert _fingerprint(one) != _fingerprint(two)
        assert [j.occurrence for j in assign_occurrences([one, two])] == [1, 1]

    def test_assign_occurrences_counts_distinct_links_only(self):
        one = _job(A, url="https://www.xing.com/m/x1")
        two = _job(A, url="https://www.xing.com/m/x2")
        other = _job(B, url="https://www.xing.com/m/x3")
        repeat = _job(A, url="https://www.xing.com/m/x1")
        assigned = assign_occurrences([one, two, other, repeat])
        assert [j.occurrence for j in assigned] == [1, 2, 1]

    def test_assign_occurrences_ignores_case_and_whitespace(self):
        one = _job("Gehalt: 50.000", url="https://www.xing.com/m/x1")
        two = _job("  gehalt:  50.000 ", url="https://www.xing.com/m/x2")
        assert [j.occurrence for j in assign_occurrences([one, two])] == [1, 2]

    def test_assign_occurrences_is_order_independent_for_identical_blocks(self):
        one = _job(A, url="https://www.xing.com/m/x1")
        two = _job(A, url="https://www.xing.com/m/x2")
        forward = [j.occurrence for j in assign_occurrences([one, two])]
        backward = [j.occurrence for j in assign_occurrences([two, one])]
        assert sorted(forward) == sorted(backward) == [1, 2]


class TestProtectedRows:
    def test_operator_touched_record_is_not_rewritten(self, factory):
        with factory() as db:
            first, _ = upsert_job(db, _job(A), _score(80, "APPLY"))
            first.status = "APPLIED"
            db.commit()
            outcome = upsert_job_observed(db, _job(A), _score(5, "SKIP"))
            assert outcome.frozen and not outcome.possible_collision
            row = db.get(JobRecord, first.id)
            assert (row.score, row.recommendation, row.status) == (80, "APPLY", "APPLIED")

    def test_contaminated_history_creates_a_sibling_and_never_rewrites_the_old_row(self, factory):
        """Before the identity fix a sibling's text could overwrite the first row. Such a
        row keeps its id, url, status and score; the observation of the other posting
        becomes its own row, so nothing the operator attached is changed or lost."""
        with factory() as db:
            old, _ = upsert_job(db, _job(A, url="https://www.xing.com/m/original"), _score(80))
            old.description = B  # contaminated by an earlier overwrite
            old.status = "APPLIED"
            db.commit()
            outcome = upsert_job_observed(db, _job(A), _score(10, "SKIP"))
            assert outcome.created and outcome.sibling_created
            row = db.get(JobRecord, old.id)
            assert (row.description, row.status, row.score, row.url) == (
                B,
                "APPLIED",
                80,
                "https://www.xing.com/m/original",
            )
            assert _count(db) == 2

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
            assert not outcome.frozen and not outcome.sibling_created
            row = db.get(JobRecord, first.id)
            assert (row.score, row.description, row.identity_key) == (70, "new", None)

    def test_bundesagentur_fingerprint_formula_is_unchanged(self):
        job = _job("x", source="bundesagentur", url="https://ba/1/")
        expected = hashlib.sha256(
            b"bundesagentur|stadtwerke beispiel|sachbearbeiter (m/w/d)|https://ba/1"
        ).hexdigest()
        assert _fingerprint(job) == expected

    def test_historical_insert_only_contract_still_holds(self, factory):
        with factory() as db:
            upsert_job(db, _job(A), _score())
            now = datetime.now(UTC)
            with pytest.raises(ValueError, match="historical"):
                upsert_job_observed(db, _job(A), _score(), first_seen_at=now, last_seen_at=now)

    def test_historical_import_of_a_new_sibling_is_allowed(self, factory):
        with factory() as db:
            upsert_job(db, _job(A), _score())
            then = datetime(2026, 1, 1, tzinfo=UTC)
            outcome = upsert_job_observed(
                db, _job(B), _score(), first_seen_at=then, last_seen_at=then
            )
            assert outcome.created and outcome.sibling_created

    def test_stale_identity_map_copy_cannot_hide_a_concurrent_change(self, factory):
        with factory() as db, factory() as other:
            first, _ = upsert_job(db, _job(A), _score())
            held = db.get(JobRecord, first.id)
            assert held.status == "NEW"
            row = other.get(JobRecord, first.id)
            row.status = "APPLIED"
            other.commit()
            outcome = upsert_job_observed(db, _job(A), _score(5, "SKIP"))
            assert outcome.frozen

    def test_legacy_row_without_identity_key_is_a_member_of_its_group(self, factory):
        with factory() as db:
            legacy, _ = upsert_job(db, _job(A), _score(40))
            legacy.identity_key = None
            db.commit()
            outcome = upsert_job_observed(db, _job(A), _score(70, "APPLY"))
            assert outcome.record.id == legacy.id and not outcome.created
            assert _count(db) == 1


class TestRaceRecovery:
    def test_losing_insert_re_resolves_and_becomes_a_sibling(self, factory, monkeypatch):
        """Both writers saw an empty group and chose the base fingerprint; the loser's INSERT
        hits uq_jobs_fingerprint and must NOT overwrite the winner's different evidence."""
        with factory() as winner_db, factory() as db:
            real = repositories.resolve_xing_identity
            calls = {"n": 0}

            def stale_first(session, job, *, lock):
                calls["n"] += 1
                resolution = real(session, job, lock=lock)
                if calls["n"] == 1:
                    upsert_job(winner_db, _job(A), _score(80, "APPLY"))  # rival commits the base
                return resolution

            monkeypatch.setattr(repositories, "resolve_xing_identity", stale_first)
            outcome = upsert_job_observed(db, _job(B), _score(10, "SKIP"))
            assert outcome.created and outcome.sibling_created
            assert _count(db) == 2
            base = db.execute(
                select(JobRecord).where(JobRecord.fingerprint == _fingerprint(_job(A)))
            ).scalar_one()
            assert (base.description, base.score) == (A, 80)

    def test_identical_evidence_loser_converges_on_the_winner(self, factory, monkeypatch):
        with factory() as winner_db, factory() as db:
            real = repositories.resolve_xing_identity
            calls = {"n": 0}

            def stale_first(session, job, *, lock):
                calls["n"] += 1
                resolution = real(session, job, lock=lock)
                if calls["n"] == 1:
                    upsert_job(winner_db, _job(A), _score(80, "APPLY"))
                return resolution

            monkeypatch.setattr(repositories, "resolve_xing_identity", stale_first)
            outcome = upsert_job_observed(db, _job(A), _score(60, "MAYBE"))
            assert not outcome.created and not outcome.sibling_created
            assert _count(db) == 1


class TestResolutionContract:
    def test_preliminary_lookup_reads_the_row_the_upsert_writes(self, factory):
        with factory() as db:
            a, _ = upsert_job(db, _job(A), _score())
            b = upsert_job_observed(db, _job(B), _score()).record
            assert repositories.get_job_by_fingerprint(db, _job(A)).id == a.id
            assert repositories.get_job_by_fingerprint(db, _job(B)).id == b.id
            assert repositories.get_job_by_fingerprint(db, _job(C)) is None
            assert resolve_xing_identity(db, _job(C), lock=False).sibling

    def test_score_and_persist_reports_a_new_sibling_as_a_normal_new_vacancy(self, factory):
        with factory() as db:
            record, result, created = score_and_persist(db, SKILLS, _job(A))
            assert created and not result.sibling_posting
            record2, result2, created2 = score_and_persist(db, SKILLS, _job(B))
            assert created2 and result2.sibling_posting and not result2.possible_collision
            assert record2.id != record.id
            assert not db.dirty

    def test_score_and_persist_ambiguity_reports_persisted_values(self, factory):
        with factory() as db:
            score_and_persist(db, SKILLS, _job(A))
            score_and_persist(db, SKILLS, _job(B))
            record, result, created = score_and_persist(db, SKILLS, _job(""))
            assert not created and result.possible_collision and result.is_duplicate
            assert (result.score, result.recommendation) == (record.score, record.recommendation)
