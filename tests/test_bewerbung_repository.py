import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.bewerbung_repository import (
    create_bewerbung_draft,
    get_bewerbung_draft_by_id,
    get_latest_bewerbung_draft,
    to_bewerbung_draft,
)
from app.db.models import BewerbungDraftRecord
from app.models.bewerbung import BewerbungDraftData, BewerbungParagraph, BewerbungProviderPlan


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _plan(**overrides) -> BewerbungProviderPlan:
    defaults = dict(
        opening_style="ROLE_INTEREST",
        paragraphs=[{"kind": "EVIDENCE", "claim_ids": ["candidate_skill:1"]}],
        closing_style="INTERVIEW_INTEREST",
    )
    defaults.update(overrides)
    return BewerbungProviderPlan(**defaults)


def _data(**overrides) -> BewerbungDraftData:
    defaults = dict(
        job_id=1,
        cv_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="fp-1",
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v1",
        provider="deterministic",
        subject="Bewerbung als Python Developer",
        salutation="Sehr geehrte Damen und Herren,",
        opening="mit Interesse habe ich Ihre Anzeige gelesen.",
        body_paragraphs=[
            BewerbungParagraph(
                text="Ich bringe Kenntnisse in Python mit.",
                source_claim_ids=["candidate_skill:1"],
            )
        ],
        closing="Ich freue mich auf ein Gespräch.",
        signature_name="Anna Example",
        plan=_plan(),
    )
    defaults.update(overrides)
    return BewerbungDraftData(**defaults)


# --- create / round trip ----------------------------------------------------


def test_create_bewerbung_draft_persists_and_round_trips():
    db = _db()
    record = create_bewerbung_draft(db, _data())

    response = to_bewerbung_draft(record)
    assert response.job_id == 1
    assert response.cv_draft_id == 1
    assert response.match_id == 1
    assert response.subject == "Bewerbung als Python Developer"
    assert response.status == "DRAFT"
    assert response.id == record.id


def test_get_bewerbung_draft_by_id_returns_exact_snapshot():
    db = _db()
    record = create_bewerbung_draft(db, _data())
    found = get_bewerbung_draft_by_id(db, record.id)
    assert found is not None
    assert found.id == record.id


def test_get_bewerbung_draft_by_id_returns_none_for_unknown_id():
    db = _db()
    assert get_bewerbung_draft_by_id(db, 999) is None


def test_get_latest_bewerbung_draft_returns_most_recent_row():
    db = _db()
    first = create_bewerbung_draft(db, _data(cv_draft_id=1))
    second = create_bewerbung_draft(db, _data(cv_draft_id=2))

    latest = get_latest_bewerbung_draft(db, 1)
    assert latest.id == second.id
    assert latest.id != first.id


def test_get_latest_bewerbung_draft_returns_none_when_none_exists():
    db = _db()
    assert get_latest_bewerbung_draft(db, 999) is None


# --- regeneration always creates a new row (spec section 35) ---------------


def test_identical_inputs_always_create_a_new_row_not_a_cache_hit():
    """Unlike CandidateJobMatch/CandidateCVDraft, Bewerbung generation has
    no cache identity — two calls with byte-identical BewerbungDraftData
    must produce two distinct rows, never be collapsed into one."""
    db = _db()
    data = _data()
    first = create_bewerbung_draft(db, data)
    second = create_bewerbung_draft(db, data)

    assert first.id != second.id
    total = db.scalar(select(func.count()).select_from(BewerbungDraftRecord))
    assert total == 2


# --- immutability of generation-time metadata -------------------------------


def test_draft_is_immutable_snapshot_of_generation_time_metadata():
    db = _db()
    record = create_bewerbung_draft(
        db,
        _data(
            candidate_profile_version=4,
            match_algorithm_version="v1",
            cv_adapter_version="v1",
        ),
    )
    response = to_bewerbung_draft(record)
    assert response.candidate_profile_version == 4
    assert response.match_algorithm_version == "v1"
    assert response.cv_adapter_version == "v1"
    assert response.bewerbung_generator_version == "v1"


def test_claims_survive_json_round_trip():
    from app.models.bewerbung import AllowedClaim

    db = _db()
    claim = AllowedClaim(
        id="candidate_skill:1", claim="Python", source_entity="candidate_skill", source_id=1
    )
    record = create_bewerbung_draft(db, _data(claims=[claim]))
    response = to_bewerbung_draft(record)
    assert len(response.claims) == 1
    assert response.claims[0].claim == "Python"
    assert response.claims[0].source_id == 1


def test_privacy_safe_draft_json_contains_no_extra_fields():
    db = _db()
    record = create_bewerbung_draft(db, _data())
    payload = json.loads(record.draft_json)
    assert set(payload.keys()) == set(BewerbungDraftData.model_fields.keys())
