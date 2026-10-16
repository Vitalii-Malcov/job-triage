"""Stage 14B Codex acceptance-review reproductions (matcher v4).

P1-1 mixed-line requirement loss, P1-2 experience years bound to subject,
P2-1 "z. B." examples are one grouped requirement, P2-2 application/contact
footer boundaries, P2-3 unrelated year counts, P2-4 OTHER dedup -- plus the
automation-threshold safety check and the historical-timestamp concurrency
winner branch of upsert_job.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.repositories as repositories
from app.agents.candidate_job_matcher import JobMatchInput, compute_match
from app.agents.requirement_extractor import extract_experience_requirement
from app.agents.skill_extractor import extract_skills
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import upsert_job
from app.models.candidate_profile import (
    CandidateExperience,
    CandidateJobPreferences,
    CandidateProfile,
    CandidateSkill,
)
from app.models.job import Job, JobScore

AS_OF = date(2026, 10, 15)


def _profile(skills=(), experiences=()) -> CandidateProfile:
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        job_preferences=CandidateJobPreferences(),
        skills=[CandidateSkill(id=i, name=name) for i, name in enumerate(skills, 1)],
        experiences=list(experiences),
    )


def _experience(id_, start, end, technologies):
    return CandidateExperience(
        id=id_,
        company=f"Company {id_}",
        job_title="Developer",
        start_date=start,
        end_date=end,
        is_current=end is None,
        technologies=list(technologies),
    )


def _match(description, profile, must=(), title="Developer"):
    job = JobMatchInput(job_id=1, title=title, description=description, must_have_skills=list(must))
    return compute_match(job, profile, company_research_id=None, as_of=AS_OF)


def _all(result):
    return [
        *result.matched_requirements,
        *result.partial_requirements,
        *result.missing_requirements,
        *result.unknown_requirements,
    ]


def _others(result):
    return [r for r in _all(result) if r.requirement_type == "OTHER"]


def _by_norm(result):
    return {r.normalized_requirement: r for r in _all(result)}


# --- P1-1 mixed-line requirement loss -------------------------------------------


def test_python_und_sap_abap_keeps_the_unrecognized_part():
    result = _match("Profil:\n- Kenntnisse in Python und SAP ABAP", _profile(["Python"]))
    assert _by_norm(result)["python"].match_status == "MATCH"
    others = _others(result)
    assert [(o.requirement, o.importance, o.match_status) for o in others] == [
        ("SAP ABAP", "REQUIRED", "UNKNOWN")
    ]
    assert "HUMAN_REVIEW_REQUIRED" in others[0].reason


def test_python_docker_and_proprietary_platform():
    description = "Profil:\n- Kenntnisse in Python, Docker und proprietäre XYZ-Plattform"
    result = _match(description, _profile(["Python", "Docker"]))
    reqs = _by_norm(result)
    assert reqs["python"].match_status == reqs["docker"].match_status == "MATCH"
    assert [o.requirement for o in _others(result)] == ["proprietäre XYZ-Plattform"]


def test_fully_recognized_line_has_no_redundant_other():
    result = _match("Profil:\n- Kenntnisse in Python und Docker", _profile(["Python", "Docker"]))
    assert _others(result) == []
    assert result.required_skill_score == 100


def test_entirely_unknown_line_is_one_whole_other():
    result = _match("Profil:\n- Kenntnisse in SAP ABAP", _profile(["Python"]))
    assert [o.requirement for o in _others(result)] == ["Kenntnisse in SAP ABAP"]


def test_qualified_and_compound_technology_mentions_keep_only_material_residual():
    # v5: "API-Entwicklung (REST)" and "Git-basierter" are consumed by their
    # technology; the domain "Softwareentwicklung" and the noun before an
    # unrelated parenthesis stay material (final Codex review P1-2).
    description = (
        "Profil:\n"
        "- Erfahrung mit API-Entwicklung (REST) und Git-basierter Softwareentwicklung.\n"
        "- Grundverständnis von Software-Engineering-Prinzipien (Testing, CI/CD)."
    )
    # v12: the experience WITH "API-Entwicklung (REST)" is its own requirement
    assert [o.requirement for o in _others(_match(description, _profile(["Git"])))] == [
        "Erfahrung mit API-Entwicklung (REST)",
        "Softwareentwicklung",
        "Software-Engineering-Prinzipien",
    ]


def test_mixed_unknown_requirement_cannot_reach_the_shortlist_threshold():
    """Codex reproduction: Python + unknown SAP ABAP must not qualify for
    the automation shortlist merely because SAP ABAP vanished."""
    threshold = Settings.model_fields["automation_shortlist_min_match_score"].default
    profile = _profile(["Python"], [_experience(1, date(2018, 1, 1), None, ["Python"])])

    python_only = _match("Profil:\n- Kenntnisse in Python", profile)
    mixed = _match("Profil:\n- Kenntnisse in Python und SAP ABAP", profile)

    assert python_only.overall_score >= threshold  # the vanished-requirement outcome
    assert mixed.overall_score < threshold
    assert mixed.required_skill_score == 50


# --- P1-2 experience years bound to the requirement's subject -------------------

_PYTHON_3Y = "Profil:\n- Mindestens 3 Jahre Berufserfahrung mit Python\n- Kenntnisse in Git"


def test_git_only_work_never_satisfies_python_years():
    profile = _profile(
        ["Python", "Git"], [_experience(1, date(2020, 1, 1), date(2024, 1, 1), ["Git"])]
    )
    experience = _by_norm(_match(_PYTHON_3Y, profile))["experience-3y"]
    assert experience.match_status != "MATCH"
    assert experience.match_status == "MISSING"
    assert experience.candidate_evidence == []


def test_python_work_of_four_years_satisfies_three_plus_years():
    description = "Profil:\n- 3+ years of professional experience with Python"
    profile = _profile(["Python"], [_experience(1, date(2020, 1, 1), date(2024, 1, 1), ["Python"])])
    experience = _by_norm(_match(description, profile))["experience-3y"]
    assert experience.match_status == "MATCH"
    assert [ref.entity_id for ref in experience.candidate_evidence] == [1]


def test_python_work_without_dates_is_unknown():
    profile = _profile(["Python"], [_experience(1, None, None, ["Python"])])
    profile.experiences[0] = profile.experiences[0].model_copy(update={"is_current": False})
    assert _by_norm(_match(_PYTHON_3Y, profile))["experience-3y"].match_status == "UNKNOWN"


def test_domain_only_years_never_match():
    description = "Profil:\n- 3 years of professional experience in software development"
    profile = _profile([], [_experience(1, date(2010, 1, 1), None, ["Python"])])
    experience = _by_norm(_match(description, profile))["experience-3y"]
    assert experience.match_status == "UNKNOWN"
    assert "software development" in experience.requirement


# --- P2-1 "z. B." examples ------------------------------------------------------

_FRAMEWORKS = (
    "Profil:\n- Kenntnisse in Python und modernen KI-Frameworks (z. B. LangChain, LlamaIndex)."
)


def test_examples_are_one_grouped_requirement_not_two_hard_missing():
    result = _match(_FRAMEWORKS, _profile(["Python"]), must=["python"])
    reqs = _by_norm(result)
    assert "langchain" not in reqs and "llamaindex" not in reqs
    group = reqs["group:modernen ki-frameworks"]
    assert group.requirement == "modernen KI-Frameworks (examples: LangChain, LlamaIndex)"
    assert (group.importance, group.match_status) == ("REQUIRED", "UNKNOWN")
    assert [m.requirement for m in result.missing_requirements] == []
    assert _others(result) == []


def test_a_trusted_named_example_supports_the_group():
    group = _by_norm(_match(_FRAMEWORKS, _profile(["Python", "LangChain"])))[
        "group:modernen ki-frameworks"
    ]
    assert group.match_status == "MATCH"
    assert [ref.value for ref in group.candidate_evidence] == ["LangChain"]


def test_stored_example_skills_are_not_resurrected_unless_in_title():
    stored = ["python", "langchain", "llamaindex"]
    reqs = _by_norm(_match(_FRAMEWORKS, _profile(["Python"]), must=stored))
    assert "langchain" not in reqs and "llamaindex" not in reqs
    titled = _by_norm(
        _match(_FRAMEWORKS, _profile(["Python"]), must=stored, title="LangChain Developer")
    )
    assert titled["langchain"].importance == "REQUIRED"


@pytest.mark.parametrize(
    "line",
    [
        "Erfahrung mit Cloud-Plattformen wie z. B. AWS oder Azure",
        "Erfahrung mit Cloud-Plattformen, zum Beispiel AWS oder Azure",
        "Experience with cloud platforms such as AWS or Azure",
        "Experience with cloud platforms (e.g. AWS, Azure)",
    ],
)
def test_example_markers_variants(line):
    result = _match(f"Profil:\n- {line}", _profile(["AWS"]))
    reqs = _by_norm(result)
    assert "azure" not in reqs
    groups = [r for r in _all(result) if r.normalized_requirement.startswith("group:")]
    assert len(groups) == 1 and groups[0].match_status == "MATCH"


def test_ordinary_wording_still_yields_individual_requirements():
    result = _match("Profil:\n- Kenntnisse in LangChain und LlamaIndex", _profile([]))
    reqs = _by_norm(result)
    assert reqs["langchain"].importance == reqs["llamaindex"].importance == "REQUIRED"
    assert reqs["langchain"].match_status == "MISSING"


# --- P2-2 application/contact footer boundaries ---------------------------------


@pytest.mark.parametrize(
    "heading",
    [
        "Jetzt bewerben:",
        "Jetzt bewerben!",
        "Bewerben Sie sich",
        "Bewirb dich jetzt:",
        "Ihre Bewerbung:",
        "Deine Bewerbung",
        "Bewerbung:",
        "Kontakt:",
        "Ansprechpartner:",
        "Apply now!",
        "Application:",
        "How to apply:",
    ],
)
def test_application_footer_does_not_inherit_profile_context(heading):
    description = (
        "Profil:\n- Kenntnisse in Python\n"
        f"{heading}\n- Senden Sie Ihre Unterlagen per E-Mail an jobs@example.com"
    )
    result = _match(description, _profile(["Python"]))
    assert [r.normalized_requirement for r in _all(result)] == ["python"]


def test_nested_requirement_heading_is_not_a_closing():
    description = "Profil:\nTechnologien:\n- Python\n- Docker"
    assert extract_skills(None, description).must_have_skills == ["docker", "python"]


# --- P2-3 unrelated year counts -------------------------------------------------


@pytest.mark.parametrize(
    "text,years",
    [
        ("3 Jahre Berufserfahrung", 3),
        ("mindestens drei Jahre Berufserfahrung", 3),
        ("3+ years of professional experience", 3),
        ("at least 5 years experience", 5),
        ("2-jährige Berufserfahrung", 2),
        ("Berufserfahrung von mindestens 4 Jahren", 4),
    ],
)
def test_years_tightly_bound_to_experience_are_extracted(text, years):
    requirement = extract_experience_requirement(None, f"Profil:\n- {text}")
    assert requirement is not None and requirement.years == years


@pytest.mark.parametrize(
    "text",
    [
        "Sie haben Erfahrung mit Python in unserem seit 20 Jahren bestehenden Team",
        "Unternehmen mit 30 Jahren Erfahrung am Markt",
        "seit 10 Jahren entwickeln wir Software",
    ],
)
def test_unrelated_year_counts_are_not_experience_years(text):
    assert extract_experience_requirement(None, f"Profil:\n- {text}") is None


# --- P2-4 OTHER dedup -----------------------------------------------------------


def test_repeated_unknown_lines_count_once():
    single = _match(
        "Profil:\n- Kenntnisse in SAP ABAP\n- Kenntnisse in Python", _profile(["Python"])
    )
    repeated = _match(
        "Profil:\n- Kenntnisse in SAP ABAP\n- Kenntnisse in Python\n- KENNTNISSE in  SAP   abap",
        _profile(["Python"]),
    )
    others = _others(repeated)
    assert len(others) == 1
    assert len(others[0].job_evidence) == 2
    assert repeated.required_skill_score == single.required_skill_score == 50


def test_different_unknown_requirements_are_not_merged():
    result = _match("Profil:\n- Kenntnisse in SAP ABAP\n- Kenntnisse in SAP HANA", _profile([]))
    assert len(_others(result)) == 2


# --- upsert_job historical timestamps: concurrency-winner branch ------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_historical_timestamps_refused_when_a_concurrent_winner_exists(db, monkeypatch):
    posting = Job(
        source="test", title="AI Engineer", company="Example", url="https://example.com/1"
    )
    score = JobScore(score=83, recommendation="APPLY")
    winner, _ = upsert_job(db, posting, score)
    original = (winner.first_seen_at, winner.last_seen_at)

    real_lookup = repositories.get_job_by_fingerprint
    calls = {"n": 0}

    def _miss_once(session, job):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(session, job)

    # Simulate the race: the pre-insert lookup misses, the INSERT then hits
    # uq_jobs_fingerprint and the recovery path finds the winner.
    monkeypatch.setattr(repositories, "get_job_by_fingerprint", _miss_once)
    with pytest.raises(ValueError, match="new job record"):
        upsert_job(
            db,
            posting,
            score,
            first_seen_at=datetime(2026, 9, 15, 13, 31),
            last_seen_at=datetime(2026, 9, 15, 19, 40),
        )
    assert calls["n"] == 2
    db.rollback()
    db.refresh(winner)
    assert (winner.first_seen_at, winner.last_seen_at) == original
