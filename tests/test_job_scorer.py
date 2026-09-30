from app.agents.job_scorer import JobScorer
from app.models.job import Job


def make_job(**overrides):
    data = {
        "source": "test",
        "title": "Junior Fast API Python Developer",
        "company": "Example GmbH",
        "url": "https://example.com/job/1",
        "description": ("We build APIs with Python and FastAPI. " * 15),
    }
    data.update(overrides)
    return Job(**data)


def test_aliases_and_must_have_weighting():
    scorer = JobScorer({"python", "fastapi", "git"})
    result = scorer.score(
        make_job(
            must_have_skills=["Python", "Fast API"],
            nice_to_have_skills=["Docker", "Git"],
        )
    )
    assert result.recommendation == "APPLY"
    assert "fastapi" in result.matched_must_have
    assert "docker" in result.missing_skills


def test_missing_majority_of_must_have_is_skip():
    scorer = JobScorer({"python"})
    result = scorer.score(make_job(must_have_skills=["Python", "FastAPI", "Docker", "AWS"]))
    assert result.recommendation == "SKIP"


def test_low_data_confidence_overrides_a_high_numeric_score():
    scorer = JobScorer({"python"})
    result = scorer.score(make_job(description="", must_have_skills=["Python"]))

    assert result.score >= 80
    assert result.data_confidence == 0.1
    assert result.recommendation == "NEEDS_ENRICHMENT"


def test_single_must_have_match_cannot_reach_apply_even_with_rich_description():
    # Stage 10 finding: alfatraining's "Programmierung mit Python" course
    # listing scored 91/APPLY purely because its extracted must-have set
    # was a single skill ("python"), trivially matched -- combined with a
    # long, skill-keyword-dense description (a real course syllabus, not
    # a requirements list) that kept data_confidence high. A must-have set
    # this sparse must never support an automatic APPLY, no matter how
    # rich the surrounding text is.
    scorer = JobScorer({"python", "fastapi", "flask", "sqlalchemy", "postgresql", "docker"})
    description = (
        "In diesem Kurs behandeln wir Python, FastAPI, Flask, SQLAlchemy, "
        "PostgreSQL und Docker im Detail. " * 20
    )
    result = scorer.score(
        make_job(
            title="Programmierung mit Python",
            description=description,
            must_have_skills=["Python"],
            nice_to_have_skills=[],
        )
    )
    assert result.score >= 80
    assert result.recommendation != "APPLY"


def test_empty_nice_to_have_set_is_not_treated_as_fully_satisfied():
    # Stage 10 finding: nice_score used to default to 1.0 (full credit)
    # for an EMPTY nice-to-have set -- absence of evidence is not proof of
    # satisfaction. A job with a borderline must-coverage and zero
    # extracted nice-to-haves should score lower than one with the exact
    # same must-coverage plus genuinely matched nice-to-haves.
    scorer = JobScorer({"python", "fastapi", "docker"})
    with_nice = scorer.score(
        make_job(
            must_have_skills=["Python", "FastAPI"],
            nice_to_have_skills=["Docker"],
        )
    )
    without_nice = scorer.score(
        make_job(
            must_have_skills=["Python", "FastAPI"],
            nice_to_have_skills=[],
        )
    )
    assert without_nice.score < with_nice.score


# --- H1 (Astra Stage 12 audit): optional evidence must never become -------
# --- must-have evidence solely because must_have_skills is empty ----------


def test_optional_only_evidence_cannot_reach_apply():
    # Astra's exact repro: "Python and PostgreSQL are optional." extracts
    # to must=[], nice=[postgresql, python]. The ingestion union `job.skills`
    # (must | nice | source skills) still contains both -- the pre-fix
    # legacy fallback resolved that union as must-have and reached 100/APPLY.
    scorer = JobScorer({"python", "postgresql"})
    result = scorer.score(
        make_job(
            title="Backend Developer",
            description="Python and PostgreSQL are optional. " * 15,
            skills=["postgresql", "python"],
            must_have_skills=[],
            nice_to_have_skills=["postgresql", "python"],
        )
    )
    assert result.recommendation != "APPLY"


def test_optional_only_evidence_is_not_counted_as_must_coverage():
    scorer = JobScorer({"python", "postgresql"})
    result = scorer.score(
        make_job(
            skills=["postgresql", "python"],
            must_have_skills=[],
            nice_to_have_skills=["postgresql", "python"],
        )
    )
    assert result.matched_must_have == []
    assert result.missing_must_have == []
    assert set(result.matched_nice_to_have) == {"postgresql", "python"}


def test_genuine_two_must_python_postgresql_still_reaches_apply():
    # The legitimate counterpart: EXPLICIT must-have evidence must still
    # score normally -- the H1 fix only removes the legacy-fallback path,
    # never explicit must_have_skills.
    scorer = JobScorer({"python", "postgresql"})
    result = scorer.score(
        make_job(
            title="Backend Developer",
            description="Build backend services with Python and PostgreSQL. " * 10,
            skills=["postgresql", "python"],
            must_have_skills=["postgresql", "python"],
            nice_to_have_skills=[],
        )
    )
    assert result.recommendation == "APPLY"
    assert set(result.matched_must_have) == {"postgresql", "python"}


def test_alias_dedup_does_not_manufacture_extra_must_signals_from_optional():
    # Two textual aliases of the SAME underlying skill, both nice-to-have
    # only -- must never resolve to a two-signal must-have set (which
    # would clear MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY) via the legacy
    # fallback.
    scorer = JobScorer({"rest"})
    result = scorer.score(
        make_job(
            skills=["REST API", "rest"],
            must_have_skills=[],
            nice_to_have_skills=["REST API", "rest"],
        )
    )
    assert result.matched_must_have == []
    assert result.recommendation != "APPLY"


def test_unclassified_source_skills_with_empty_must_and_nice_cannot_reach_apply():
    # Astra round-2 H1 repro, at the scorer level. This exact shape --
    # `skills` populated, BOTH extraction categories empty,
    # skill_source="description_extracted" -- is what the CURRENT
    # extractor produces for a description that merely MENTIONS
    # technologies without stating them as requirements. The removed
    # fallback used to resolve `skills` as the must-have set here and
    # reach 90/APPLY. Empty must/nice is NOT evidence of historical
    # provenance, so there is no fallback left to reach.
    scorer = JobScorer({"python", "postgresql"})
    result = scorer.score(
        make_job(
            title="Backend Developer",
            description=(
                "Our platform uses Python and PostgreSQL to support customers "
                "around the world. " * 15
            ),
            skills=["python", "postgresql"],
            must_have_skills=[],
            nice_to_have_skills=[],
            skill_source="description_extracted",
        )
    )
    assert result.matched_must_have == []
    assert result.missing_must_have == []
    assert result.recommendation != "APPLY"


def test_unclassified_source_skills_are_still_visible_as_descriptive_evidence():
    # Removing the fallback must not DELETE the source skills from the
    # export/explanation surface -- they simply stop counting as
    # MANDATORY evidence. They remain in matched_skills/missing_skills
    # (and still feed data_confidence).
    scorer = JobScorer({"python"})
    result = scorer.score(
        make_job(
            title="Backend Developer",
            description=("Our platform uses Python and PostgreSQL. " * 15),
            skills=["python", "postgresql"],
            must_have_skills=[],
            nice_to_have_skills=[],
            skill_source="description_extracted",
        )
    )
    assert "python" in result.matched_skills
    assert "postgresql" in result.missing_skills
    assert result.matched_must_have == []


def test_descriptive_mentions_alone_never_promote_to_must_have():
    # The same shape with skill_source left unset (the pre-enrichment
    # NULL that historical rows also carry): still no promotion. The fix
    # does not depend on any provenance marker, precisely because no
    # trustworthy one exists -- see JobScorer.score's own comment.
    scorer = JobScorer({"python", "sql", "docker"})
    result = scorer.score(
        make_job(
            title="Backend Developer",
            description=("We build backend services with Python, SQL and Docker. " * 15),
            skills=["python", "sql", "docker"],
            must_have_skills=[],
            nice_to_have_skills=[],
            skill_source=None,
        )
    )
    assert result.matched_must_have == []
    assert result.missing_must_have == []
    assert result.recommendation != "APPLY"


def test_no_skills_shape_whatsoever_can_synthesize_must_have_evidence():
    # Exhaustive statement of the invariant: across every combination of
    # source-skill content, the resolved must-have set is EXACTLY
    # `must_have_skills` -- never borrowed from `skills` or
    # `nice_to_have_skills`.
    scorer = JobScorer({"python", "postgresql", "docker"})
    for skills, nice in (
        (["python", "postgresql"], []),
        (["python", "postgresql"], ["docker"]),
        (["docker"], ["python", "postgresql"]),
        ([], []),
    ):
        result = scorer.score(
            make_job(
                title="Backend Developer",
                description=("Python, PostgreSQL and Docker are used here. " * 15),
                skills=skills,
                must_have_skills=[],
                nice_to_have_skills=nice,
            )
        )
        assert result.matched_must_have == [], (skills, nice)
        assert result.missing_must_have == [], (skills, nice)
        assert result.recommendation != "APPLY", (skills, nice)


def test_legitimate_junior_python_backend_developer_still_scores_apply():
    # A real job with a normal (non-sparse) extracted must-have set must
    # be completely unaffected by the Stage 10 sparse-evidence fixes.
    scorer = JobScorer(
        {"python", "fastapi", "sqlalchemy", "postgresql", "git", "pytest", "docker", "rest-api"}
    )
    result = scorer.score(
        make_job(
            title="Junior Python Backend Developer",
            description=(
                "We are looking for a Junior Python Backend Developer to build "
                "REST APIs with FastAPI and SQLAlchemy against PostgreSQL. " * 10
            ),
            must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
            nice_to_have_skills=["Docker", "Pytest"],
        )
    )
    assert result.recommendation == "APPLY"
