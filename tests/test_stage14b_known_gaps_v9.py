"""Stage 14B known v8 gaps (matcher v9).

1. A technology hyphenated to an experience head ("gute Python-Erfahrung")
   lost the experience qualification: the technology span consumed
   "-Erfahrung".
2. The head-anchored clause scan stopped after five modifier words, so a
   longer chain of unknown modifiers merged a requirement into the previous
   clause.

Module attributes are used (not `from ... import`) so this file also runs
against the previous implementation and fails per test, not at collection.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.agents.candidate_job_matcher as matcher
import app.agents.requirement_extractor as rx
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint, create_match
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import CandidateJobMatchRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatchData
from app.models.candidate_profile import (
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateProfile,
    CandidateProject,
    CandidateSkill,
)
from app.services.candidate_preparation import prepare_candidate_job_match

AS_OF = date(2026, 10, 15)


def _profile(skills=(), experiences=(), education=(), projects=()):
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(skills, 1)],
        experiences=list(experiences),
        education=list(education),
        projects=list(projects),
    )


def _work(technologies):
    return CandidateExperience(
        id=1,
        company="Company",
        job_title="Developer",
        start_date=date(2018, 1, 1),
        is_current=True,
        technologies=list(technologies),
    )


def _degree(field):
    return CandidateEducation(id=1, institution="Uni", field_of_study=field, completed=True)


def _match(description, profile):
    job = matcher.JobMatchInput(job_id=1, title="Developer", description=description)
    return matcher.compute_match(job, profile, company_research_id=None, as_of=AS_OF)


def _all(result):
    return [
        *result.matched_requirements,
        *result.partial_requirements,
        *result.missing_requirements,
        *result.unknown_requirements,
    ]


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


def _profil(*lines: str) -> str:
    return "Profil:\n" + "\n".join(f"- {line}" for line in lines)


def _threshold() -> int:
    return Settings.model_fields["automation_shortlist_min_match_score"].default


def _skill_and_project(name):
    return _profile([name], projects=[CandidateProject(id=1, name="Tool", technologies=[name])])


# --- gap 1: hyphenated technology experience ----------------------------------------------

HYPHENATED = [
    ("Python-Erfahrung", "Python", "python"),
    ("gute Python-Erfahrung", "Python", "python"),
    ("mehrjährige Python-Erfahrung", "Python", "python"),
    ("praktische Python-Erfahrung", "Python", "python"),
    ("fundierte Python-Erfahrung", "Python", "python"),
    ("langjährige Python-Erfahrung", "Python", "python"),
    ("blorpige Python-Erfahrung", "Python", "python"),  # no list has the modifier
    ("gute Docker-Erfahrung", "Docker", "docker"),
    ("fundierte Kubernetes-Erfahrungen", "Kubernetes", "kubernetes"),
    ("Kenntnisse und gute Python-Erfahrung", "Python", "python"),
]


@pytest.mark.parametrize(("line", "skill", "norm"), HYPHENATED)
def test_hyphenated_experience_is_its_own_requirement(line, skill, norm):
    """The technology is the SKILL requirement; the experience compound is
    REQUIRED/UNKNOWN -- a skill and a project never establish it."""
    result = _match(_profil(line), _skill_and_project(skill))
    reqs = _by_norm(result)
    kept = line.removeprefix("Kenntnisse und ")
    assert set(reqs) == {norm, kept.casefold()}
    assert reqs[norm].match_status == "MATCH"
    other = reqs[kept.casefold()]
    assert (other.requirement, other.requirement_type) == (kept, "OTHER")
    assert (other.importance, other.match_status) == ("REQUIRED", "UNKNOWN")
    assert "HUMAN_REVIEW_REQUIRED" in other.reason
    assert result.overall_score == 70
    assert result.overall_score < _threshold()


def test_hyphenated_experience_is_not_evaluated_by_professional_work_either():
    profile = _profile(["Python"], [_work(["Python"])])
    reqs = _by_norm(_match(_profil("gute Python-Erfahrung"), profile))
    assert reqs["gute python-erfahrung"].match_status == "UNKNOWN"


def test_hyphenated_experience_threshold_safety():
    result = _match(_profil("mehrjährige Python-Erfahrung"), _skill_and_project("Python"))
    assert result.overall_score != 100
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    "line",
    [
        "Python-Erfahrung von mindestens 3 Jahren",
        "Mindestens 3 Jahre Berufserfahrung mit Python",
    ],
)
def test_a_typed_duration_owns_the_experience_without_a_duplicate(line):
    reqs = _by_norm(_match(_profil(line), _skill_and_project("Python")))
    assert set(reqs) == {"python", "experience-3y"}


@pytest.mark.parametrize(
    "line",
    ["Python-Kenntnisse", "Git-basierte Softwareentwicklung", "Erfahrung mit Python"],
)
def test_knowledge_and_attached_parts_still_belong_to_the_technology(line):
    result = _match(_profil(line), _skill_and_project("Python"))
    assert [r.requirement_type for r in _all(result)].count("OTHER") == (
        1 if line.startswith("Git") else 0
    )


# --- gap 2: no modifier-count limit ---------------------------------------------------------

LONG_DE = "äußerst umfassende technisch orientierte praktische nachweisbare"
LONG_EN = "extremely broad technically oriented practical demonstrable"
LONGER_DE = "wirklich sehr äußerst umfassende tiefgreifende technisch orientierte praktisch"


def test_long_modifier_chains_are_not_listed_anywhere():
    words = f"{LONG_DE} {LONG_EN} {LONGER_DE}".split()
    assert len(LONG_DE.split()) > 5 and len(LONGER_DE.split()) > 5
    assert not getattr(rx, "CLAUSE_MODIFIER", None)
    assert all(rx.REQUIREMENT_HEAD.fullmatch(w) is None for w in words)


HEADS = [
    "Kenntnisse", "Fachkenntnisse", "Erfahrung", "Berufserfahrung", "Praxiserfahrung",
    "Verständnis", "knowledge", "experience", "proficiency", "understanding", "skills",
    "Studium", "Abschluss", "degree",
]  # fmt: skip


@pytest.mark.parametrize("head", HEADS)
def test_a_long_modifier_chain_never_hides_a_head(head):
    for modifiers in (LONG_DE, LONG_EN, LONGER_DE):
        assert rx.starts_requirement_clause(f"{modifiers} {head} in X"), modifiers
        degree = rx.extract_education_requirement(
            None,
            _profil(f"Abgeschlossenes Studium der Mathematik und {modifiers} {head} in Informatik"),
        )
        assert degree.fields == ("Mathematik",), modifiers
        line = f"3 Jahre Berufserfahrung mit Python und {modifiers} {head} mit Kubernetes"
        assert rx.extract_experience_requirement(None, _profil(line)).subjects == ("python",)


@pytest.mark.parametrize(
    "tail",
    [
        f"und {LONG_DE} Kenntnisse in Informatik",
        f"and {LONG_EN} knowledge of Computer Science",
        f", {LONGER_DE} Kenntnisse in Informatik",
    ],
)
def test_long_modifier_knowledge_clause_is_a_separate_unknown_requirement(tail):
    line = f"Abgeschlossenes Studium der Mathematik{'' if tail[0] == ',' else ' '}{tail}"
    skills = ["Python"]
    result = _match(
        _profil(line, "Kenntnisse in Python"),
        _profile(skills, [_work(skills)], [_degree("Informatik")]),
    )
    reqs = _by_norm(result)
    assert reqs["completed-degree"].requirement == "Completed degree (Mathematik or comparable)"
    assert reqs["completed-degree"].match_status == "UNKNOWN"
    others = [r for r in reqs.values() if r.requirement_type == "OTHER"]
    assert len(others) == 1
    assert (others[0].importance, others[0].match_status) == ("REQUIRED", "UNKNOWN")
    assert result.overall_score < _threshold()


@pytest.mark.parametrize("modifiers", [LONG_DE, LONGER_DE])
def test_long_modifier_experience_stays_qualified(modifiers):
    result = _match(
        _profil(f"Kenntnisse und {modifiers} Erfahrung in Python"), _skill_and_project("Python")
    )
    reqs = _by_norm(result)
    assert set(reqs) == {"python", f"{modifiers} erfahrung".casefold()}
    assert result.overall_score < _threshold()


@pytest.mark.parametrize(
    "text",
    [
        "in der Softwareentwicklung und Kenntnisse",
        f"{LONG_DE} Mathematik oder Kenntnisse",
        f"{LONG_DE} Mathematik, Kenntnisse",
        f"{LONG_DE} Teamfähigkeit mit Kenntnissen",
        LONG_DE,
    ],
)
def test_head_scan_stops_only_at_structure(text):
    assert not rx.starts_requirement_clause(text)


# --- v9 cache identity -----------------------------------------------------------------------


def test_v8_row_is_kept_and_cache_lookup_uses_v9():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime.now(UTC)
        job = JobRecord(
            fingerprint="fp-v9",
            source="bundesagentur",
            title="Developer",
            company="Acme",
            location="Berlin",
            url="https://example.com/jobs/1",
            description=_profil("gute Python-Erfahrung"),
            skills_json="[]",
            data_confidence=0.9,
            must_have_skills_json=json.dumps(["python"]),
            nice_to_have_skills_json="[]",
            score=80,
            recommendation="APPLY",
            status="NEW",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(job)
        db.commit()
        profile_version = get_or_create_candidate_profile(db).profile_version
        v8_data = CandidateJobMatchData(
            job_id=job.id,
            candidate_profile_version=profile_version,
            company_research_id=None,
            algorithm_version="v8",
            overall_score=100,
            coverage_score=100,
            required_skill_score=100,
            preferred_skill_score=100,
            experience_support_score=100,
        )
        create_match(db, v8_data, compute_job_snapshot_fingerprint(job))

        result = prepare_candidate_job_match(db, job.id, force_recompute=False)

        assert matcher.ALGORITHM_VERSION == "v9"
        assert result.algorithm_version == "v9"
        assert "gute Python-Erfahrung" in [r.requirement for r in result.unknown_requirements]
        versions = db.scalars(
            select(CandidateJobMatchRecord.algorithm_version).order_by(CandidateJobMatchRecord.id)
        ).all()
        assert versions == ["v8", "v9"]
