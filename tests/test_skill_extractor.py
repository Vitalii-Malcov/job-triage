from app.agents.skill_extractor import extract_skills


def test_extracts_literal_technologies_from_realistic_german_text():
    result = extract_skills(
        "Backend Entwickler",
        "Erfahrung mit Python, Flask und PostgreSQL ist erforderlich. "
        "Docker und Kubernetes sind wünschenswert.",
    )

    assert result.must_have_skills == ["flask", "postgresql", "python"]
    assert result.nice_to_have_skills == ["docker", "kubernetes"]
    assert result.skill_source == "description_extracted"


def test_does_not_infer_related_technologies_that_are_absent():
    result = extract_skills(
        "Junior Python Developer",
        "You will maintain Python services and collaborate with the team.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []
    assert "fastapi" not in result.must_have_skills
    assert "docker" not in result.must_have_skills
    assert "postgresql" not in result.must_have_skills


def test_empty_or_none_description_is_safe_and_keeps_extracted_provenance():
    result = extract_skills("Backend Developer", None)

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []
    assert result.skill_source == "description_extracted"


def test_literal_title_match_is_allowed_without_inferring_adjacent_skills():
    result = extract_skills("Django Developer", "")

    assert result.must_have_skills == ["django"]
    assert result.nice_to_have_skills == []


def test_explicit_german_requirement_classifies_multiple_technologies_as_must_have():
    result = extract_skills("Backend Entwickler", "Erfahrung mit Python und Django erforderlich")

    assert result.must_have_skills == ["django", "python"]
    assert result.nice_to_have_skills == []


def test_german_optional_context_classifies_skill_as_nice_to_have():
    result = extract_skills("Backend Entwickler", "Python wäre wünschenswert")

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["python"]


def test_company_stack_mentions_are_context_only():
    result = extract_skills(
        "Backend Entwickler",
        "Unser Stack besteht aus Python, PostgreSQL und Docker",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == []


def test_required_mention_wins_over_optional_mention_of_same_technology():
    result = extract_skills(
        "Backend Entwickler",
        "Python wäre wünschenswert. Erfahrung mit Python ist erforderlich.",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == []


def test_several_technologies_in_one_english_requirement_segment():
    result = extract_skills(
        "Backend Engineer",
        "Experience with Python, PostgreSQL and Docker is required",
    )

    assert result.must_have_skills == ["docker", "postgresql", "python"]
    assert result.nice_to_have_skills == []


def test_nearest_marker_separates_required_and_optional_mentions_in_one_segment():
    result = extract_skills(
        "Backend Engineer",
        "Python is required, while Docker is optional",
    )

    assert result.must_have_skills == ["python"]
    assert result.nice_to_have_skills == ["docker"]
