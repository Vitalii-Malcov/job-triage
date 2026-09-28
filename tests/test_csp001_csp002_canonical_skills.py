"""Stage 12 CSP-001 / CSP-002 regression tests.

**CSP-001 (HIGH/BLOCKING).** `app.db.candidate_profile_repository.
get_candidate_skills_for_scoring` is now the SOLE runtime source of
candidate skills for `app.agents.job_scorer.JobScorer` -- replacing the
previous split-brain architecture where JobScorer read the legacy
`app.db.models.UserProfile.skills_json` while Stage 10/11's own
seniority/domain/preference checks already read the modern
CandidateProfile. These tests prove: CandidateProfile drives scoring,
changing CandidateProfile skills changes scoring, changing the legacy
UserProfile table has ZERO effect on scoring, a missing CandidateProfile
never creates one (or falls back to any invented default skill set) as a
side effect of scoring, and every production scoring path (POST
/jobs/score, Bundesagentur, XING) loads the canonical projection ONCE per
request/collector run rather than once per vacancy.

**CSP-002 (HIGH/BLOCKING).** `app.agents.job_scorer.normalize_skill`'s
ALIASES table now folds "REST" / "rest" / "REST API" / "rest-api" to one
canonical token ("rest"), so a candidate's "REST API" skill and a job's
"rest" requirement are recognized as the SAME evidence signal.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.agents.job_scorer import normalize_skill
from app.collectors.xing_email import XingEmailBatch
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    count_candidate_profiles,
    get_candidate_skills_for_scoring,
    get_or_create_candidate_profile,
)
from app.db.models import UserProfile
from app.db.repositories import list_jobs
from app.db.session import get_db
from app.main import app
from app.models.candidate_profile import CandidateProfilePatchRequest, CandidateSkill
from app.models.job import Job
from app.services.collector_runner import run_bundesagentur, run_xing, score_and_persist


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _set_candidate_skills(db: Session, skills: list[str]) -> None:
    """Sets the singleton CandidateProfile's skills to exactly `skills`,
    fetching the current profile_version first so this works whether the
    profile already exists or not, and whether this is the first or a
    later call in the same test.
    """
    current = get_or_create_candidate_profile(db)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            skills=[CandidateSkill(name=name) for name in skills],
        ),
    )


def _job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Backend Developer",
        "company": "Example GmbH",
        "url": "https://example.com/jobs/csp-001",
        # Extraction-friendly phrasing (app.agents.skill_extractor
        # recognizes "Erfahrung mit X" as a MUST-HAVE signal) -- needed
        # because app.services.collector_runner.run_bundesagentur/run_xing
        # re-extract must_have_skills from the description and overwrite
        # whatever's passed here directly, unlike score_and_persist called
        # standalone.
        "description": "Erfahrung mit Python und Git. " * 20,
        "must_have_skills": ["python", "git"],
        "nice_to_have_skills": [],
    }
    data.update(overrides)
    return Job(**data)


def _settings(**overrides) -> Settings:
    data = dict(
        bundesagentur_api_key="upstream-key",
        xing_mailbox_username="xing-user@example.com",
        xing_mailbox_app_password="app-password",
    )
    data.update(overrides)
    return Settings(**data)


class _FakeBundesagenturCollector:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        return self._jobs

    async def fetch_detail(self, referenznummer: str) -> str | None:
        return None


class _FakeXingCollector:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.skipped_invalid_count = 0
        self.deadline_exceeded = False
        # None skips the watermark-advance step entirely (see
        # tests/test_collector_xing_endpoint.py's own FakeCollector,
        # which this mirrors).
        self.uid_validity: int | None = None
        self.confirmed_uids: list[int] = []

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        return [
            XingEmailBatch(message_id="<fake-digest@mail.xing.com>", jobs=tuple(self._jobs), uid=1)
        ]


# ============================================================================
# CSP-002: normalize_skill REST equivalence
# ============================================================================


def test_normalize_skill_rest_variants_all_equal():
    assert normalize_skill("REST") == "rest"
    assert normalize_skill("rest") == "rest"
    assert normalize_skill("REST API") == "rest"
    assert normalize_skill("rest-api") == "rest"


def test_normalize_skill_rest_distinct_from_unrelated_skills():
    assert normalize_skill("REST API") != normalize_skill("Python")
    assert normalize_skill("rest") != normalize_skill("git")


def test_normalize_skill_python_plus_rest_api_is_two_signals():
    # CSP-002 worked example: "Python + REST API = two evidence signals".
    normalized = {normalize_skill("Python"), normalize_skill("REST API")}
    assert normalized == {"python", "rest"}
    assert len(normalized) == 2


def test_normalize_skill_rest_plus_rest_api_is_one_signal():
    # CSP-002 worked example: "REST + REST API = one evidence signal".
    normalized = {normalize_skill("REST"), normalize_skill("REST API")}
    assert normalized == {"rest"}
    assert len(normalized) == 1


# ============================================================================
# CSP-001: get_candidate_skills_for_scoring -- pure, read-only projection
# ============================================================================


def test_missing_candidate_profile_returns_empty_and_creates_nothing():
    db = _db()
    result = get_candidate_skills_for_scoring(db)
    assert result == frozenset()
    assert isinstance(result, frozenset)
    assert count_candidate_profiles(db) == 0


def test_get_candidate_skills_for_scoring_returns_confirmed_skills():
    db = _db()
    _set_candidate_skills(db, ["Python", "Docker"])
    assert get_candidate_skills_for_scoring(db) == frozenset({"Python", "Docker"})


def test_get_candidate_skills_for_scoring_excludes_untrusted_skills():
    db = _db()
    current = get_or_create_candidate_profile(db)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            skills=[
                CandidateSkill(name="Python", source="MANUAL_ENTRY", confidence="CONFIRMED"),
                CandidateSkill(name="Rust", source="INFERRED", confidence="CONFIRMED"),
                CandidateSkill(name="Go", source="MANUAL_ENTRY", confidence="UNCONFIRMED"),
            ],
        ),
    )
    # Only the directly-human-asserted, CONFIRMED skill counts as evidence
    # -- mirrors app.agents.candidate_job_matcher's own trust rule exactly
    # (app.models.candidate_profile.is_usable_for_generation).
    assert get_candidate_skills_for_scoring(db) == frozenset({"Python"})


def test_get_candidate_skills_for_scoring_returns_immutable_collection():
    db = _db()
    _set_candidate_skills(db, ["Python"])
    result = get_candidate_skills_for_scoring(db)
    with pytest.raises(AttributeError):
        result.add("Java")  # frozenset has no .add -- proves immutability


# ============================================================================
# CSP-001: CandidateProfile drives JobScorer via score_and_persist
# ============================================================================


def test_candidate_profile_drives_scoring():
    db = _db()
    _set_candidate_skills(db, ["python", "git"])
    candidate_skills = get_candidate_skills_for_scoring(db)

    _record, result, _created = score_and_persist(db, candidate_skills, _job())

    assert result.matched_must_have == ["git", "python"]
    assert result.missing_must_have == []
    assert result.recommendation == "APPLY"


def test_changing_candidate_profile_skills_changes_scoring():
    db = _db()
    job = _job()

    _set_candidate_skills(db, ["cobol"])
    _record, before, _created = score_and_persist(db, get_candidate_skills_for_scoring(db), job)
    assert before.recommendation != "APPLY"
    assert before.matched_must_have == []

    _set_candidate_skills(db, ["python", "git"])
    _record2, after, _created2 = score_and_persist(db, get_candidate_skills_for_scoring(db), job)
    assert after.recommendation == "APPLY"
    assert after.matched_must_have == ["git", "python"]


def test_changing_legacy_user_profile_does_not_change_scoring():
    db = _db()
    _set_candidate_skills(db, ["python", "git"])
    baseline_skills = get_candidate_skills_for_scoring(db)

    _record, baseline_result, _created = score_and_persist(
        db, baseline_skills, _job(url="https://example.com/jobs/csp-001-legacy-1")
    )
    assert baseline_result.recommendation == "APPLY"

    # A legacy UserProfile row with a COMPLETELY DIFFERENT skill set --
    # must have zero effect on the canonical skill projection, or on
    # scoring for an equivalent job.
    legacy = UserProfile(name="default", skills_json=json.dumps(["cobol", "fortran"]))
    db.add(legacy)
    db.commit()

    skills_after_legacy_write = get_candidate_skills_for_scoring(db)
    assert skills_after_legacy_write == baseline_skills

    _record2, result2, _created2 = score_and_persist(
        db, skills_after_legacy_write, _job(url="https://example.com/jobs/csp-001-legacy-2")
    )
    assert result2.recommendation == baseline_result.recommendation
    assert result2.score == baseline_result.score
    assert result2.matched_must_have == baseline_result.matched_must_have


# ============================================================================
# CSP-001 + CSP-002: specific skill-match regressions
# ============================================================================


@pytest.mark.parametrize(
    "candidate_skill,job_requirement",
    [
        ("PostgreSQL", "PostgreSQL"),
        ("SQLAlchemy", "SQLAlchemy"),
        ("Docker", "Docker"),
        ("REST API", "rest"),
    ],
)
def test_specific_skill_matches_requirement(candidate_skill, job_requirement):
    db = _db()
    _set_candidate_skills(db, [candidate_skill, "Python"])
    candidate_skills = get_candidate_skills_for_scoring(db)
    job = _job(
        url=f"https://example.com/jobs/csp-001-{normalize_skill(candidate_skill)}",
        must_have_skills=[job_requirement, "python"],
    )

    _record, result, _created = score_and_persist(db, candidate_skills, job)

    assert normalize_skill(job_requirement) in result.matched_must_have
    assert result.missing_must_have == []


# ============================================================================
# Real AI Engineer regression (Stage 12 spec, section 5)
# ============================================================================


def test_ai_engineer_regression_matches_stage12_expected_result():
    db = _db()
    _set_candidate_skills(db, ["Python", "Git", "REST API"])
    candidate_skills = get_candidate_skills_for_scoring(db)

    description = (
        "We build reliable backend services and value strong collaboration across our teams. " * 6
    ) + " Version control workflow is handled through Git."

    job = _job(
        title="AI Engineer",
        url="https://example.com/jobs/ai-engineer-regression",
        description=description,
        must_have_skills=["git", "python", "rest"],
        nice_to_have_skills=[],
    )

    record, result, _created = score_and_persist(db, candidate_skills, job)

    assert sorted(result.matched_must_have) == ["git", "python", "rest"]
    assert result.missing_must_have == []
    assert result.score == 83
    assert result.recommendation == "APPLY"
    assert record.score == 83
    assert record.recommendation == "APPLY"


# ============================================================================
# CSP-001: production collector paths use canonical CandidateProfile skills
# ============================================================================


class TestBundesagenturCanonicalSkills:
    @pytest.mark.asyncio
    async def test_run_bundesagentur_uses_candidate_profile_skills(self, monkeypatch):
        db = _db()
        _set_candidate_skills(db, ["python", "git"])

        job = _job(url="https://example.com/jobs/ba-canonical")
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector([job]),
        )

        stats = await run_bundesagentur(db, _settings())

        assert stats["created"] == 1
        persisted = list_jobs(db)
        assert persisted[0].recommendation == "APPLY"
        assert count_candidate_profiles(db) == 1

    @pytest.mark.asyncio
    async def test_run_bundesagentur_legacy_user_profile_has_no_effect(self, monkeypatch):
        db = _db()
        _set_candidate_skills(db, ["python", "git"])
        db.add(UserProfile(name="default", skills_json=json.dumps(["cobol"])))
        db.commit()

        job = _job(url="https://example.com/jobs/ba-canonical-legacy")
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector([job]),
        )

        stats = await run_bundesagentur(db, _settings())

        assert stats["created"] == 1
        persisted = list_jobs(db)
        # If the legacy UserProfile ("cobol") were still consulted, this
        # job (must_have_skills=["python", "git"]) would not match and
        # would not reach APPLY.
        assert persisted[0].recommendation == "APPLY"

    @pytest.mark.asyncio
    async def test_run_bundesagentur_loads_candidate_skills_once_per_run(self, monkeypatch):
        db = _db()
        _set_candidate_skills(db, ["python", "git"])

        import app.services.collector_runner as collector_runner_module

        real = collector_runner_module.get_candidate_skills_for_scoring
        call_count = {"n": 0}

        def _counting(db_):
            call_count["n"] += 1
            return real(db_)

        monkeypatch.setattr(collector_runner_module, "get_candidate_skills_for_scoring", _counting)

        jobs = [_job(url=f"https://example.com/jobs/ba-once-{i}") for i in range(3)]
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: _FakeBundesagenturCollector(jobs),
        )

        stats = await run_bundesagentur(db, _settings())

        assert stats["created"] == 3
        assert call_count["n"] == 1


class TestXingCanonicalSkills:
    @pytest.mark.asyncio
    async def test_run_xing_uses_candidate_profile_skills(self, monkeypatch):
        db = _db()
        _set_candidate_skills(db, ["python", "git"])

        job = _job(source="xing", url="https://www.xing.com/m/xing-canonical-1")
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: _FakeXingCollector([job]),
        )

        stats = await run_xing(db, _settings())

        assert stats["created"] == 1
        persisted = list_jobs(db)
        assert persisted[0].recommendation == "APPLY"

    @pytest.mark.asyncio
    async def test_run_xing_loads_candidate_skills_once_per_run(self, monkeypatch):
        db = _db()
        _set_candidate_skills(db, ["python", "git"])

        import app.services.collector_runner as collector_runner_module

        real = collector_runner_module.get_candidate_skills_for_scoring
        call_count = {"n": 0}

        def _counting(db_):
            call_count["n"] += 1
            return real(db_)

        monkeypatch.setattr(collector_runner_module, "get_candidate_skills_for_scoring", _counting)

        # XING's own dedup fingerprint is (source, company, title,
        # location) -- NOT url (see
        # app.db.repositories._FINGERPRINT_FIELDS_BY_SOURCE's own
        # docstring) -- so each job needs a distinct `location`, not just
        # a distinct `url`, to avoid collapsing into one record.
        jobs = [
            _job(
                source="xing",
                url=f"https://www.xing.com/m/xing-once-{i}",
                location=f"City {i}",
            )
            for i in range(3)
        ]
        monkeypatch.setattr(
            "app.services.collector_runner.XingEmailCollector",
            lambda **kwargs: _FakeXingCollector(jobs),
        )

        stats = await run_xing(db, _settings())

        assert stats["created"] == 3
        assert call_count["n"] == 1


# ============================================================================
# CSP-001: POST /jobs/score uses canonical CandidateProfile skills
# ============================================================================

_API_KEY = "test-api-key"


@pytest.fixture()
def _endpoint_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_csp001_csp002_canonical_skills.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    fake_settings = Settings(
        api_key=_API_KEY, rate_limit_requests=1000, rate_limit_window_seconds=60
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    from app.security import rate_limit as rate_limit_module

    rate_limit_module._requests.clear()

    with TestClient(app) as test_client:
        yield test_client, session_factory

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()


def _score_job_payload(**overrides) -> dict:
    data = {
        "source": "xing",
        "title": "Backend Developer",
        "company": "Example GmbH",
        "url": "https://example.com/jobs/csp-001-endpoint",
        "description": "We build reliable backend software for our customers. " * 20,
        "skills": ["python", "git"],
    }
    data.update(overrides)
    return data


def test_jobs_score_endpoint_uses_candidate_profile_skills(_endpoint_client):
    test_client, session_factory = _endpoint_client
    _set_candidate_skills(session_factory(), ["python", "git"])

    response = test_client.post(
        "/api/v1/jobs/score",
        json=_score_job_payload(),
        headers={"X-API-Key": _API_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] == "APPLY"
    assert sorted(body["matched_must_have"]) == ["git", "python"]


def test_jobs_score_endpoint_legacy_user_profile_has_no_effect(_endpoint_client):
    test_client, session_factory = _endpoint_client
    db = session_factory()
    _set_candidate_skills(db, ["python", "git"])
    db.add(UserProfile(name="default", skills_json=json.dumps(["cobol", "fortran"])))
    db.commit()
    db.close()

    response = test_client.post(
        "/api/v1/jobs/score",
        json=_score_job_payload(url="https://example.com/jobs/csp-001-endpoint-legacy"),
        headers={"X-API-Key": _API_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    # If the legacy UserProfile ("cobol", "fortran") were still consulted,
    # this job (skills=["python", "git"]) would not match and would not
    # reach APPLY.
    assert body["recommendation"] == "APPLY"
