import pytest

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


def test_bmw_postfix_von_vorteil_applies_to_the_whole_database_list():
    result = extract_skills(
        "Softwareentwickler",
        "Erfahrung mit Datenbanken wie PostgreSQL, Oracle oder MS SQL sind von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["postgresql", "sql"]


def test_allergosan_postfix_von_vorteil_applies_to_typescript_and_rest():
    result = extract_skills(
        "Softwareentwickler",
        "Kenntnisse in Liquid, TypeScript und RESTful APIs von Vorteil.",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["rest", "typescript"]


def test_nevaris_fachkenntnisse_context_continues_into_following_bullet():
    result = extract_skills(
        "Softwareentwickler",
        "Fachkenntnisse: C/C++ oder C#, Microsoft .NET\n"
        "- Datenbanken (SQL oder ORACLE), Client-Server-Architektur",
    )

    assert result.must_have_skills == [".net", "c#", "c++", "sql"]
    assert result.nice_to_have_skills == []


def test_rewe_multiline_requirement_keeps_context_for_continued_technology_list():
    result = extract_skills(
        "SRE",
        "Strong understanding of cloud infrastructure (specifically GCP) and\n"
        "* containerization technologies (Docker, Kubernetes).\n"
        "* Solid knowledge of system administration (Linux).",
    )

    assert result.must_have_skills == ["docker", "google cloud", "kubernetes", "linux"]
    assert result.nice_to_have_skills == []


def test_agrarcommander_mit_git_vertraut_is_must_have():
    result = extract_skills(
        "Softwareentwickler",
        "Du bist mit Git sowie automatisierten Build- und Testprozessen vertraut.",
    )

    assert result.must_have_skills == ["git"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize(
    "header",
    [
        "Das bringst Du mit",
        "Das bringen Sie mit",
        "Dein Profil",
        "Ihr Profil",
        "Your profile",
        "Qualifications",
        "Qualifikationen",
        "Was sollst du mitbringen?",
    ],
)
def test_requirement_section_headers_apply_to_bullets_until_new_section(header):
    result = extract_skills(
        "Backend Engineer",
        f"{header}:\n* Python\n* Docker und Kubernetes\nUnsere Aufgaben:\n* PostgreSQL betreiben",
    )

    assert result.must_have_skills == ["docker", "kubernetes", "python"]
    assert result.nice_to_have_skills == []


@pytest.mark.parametrize("header", ["Ideal Skills", "Nice to have", "Wünschenswert"])
def test_optional_section_headers_apply_to_bullets_until_new_section(header):
    result = extract_skills(
        "Backend Engineer",
        f"{header}:\n"
        "* Experience with Python\n"
        "* Docker und Kubernetes\n"
        "Responsibilities:\n"
        "* PostgreSQL betreiben",
    )

    assert result.must_have_skills == []
    assert result.nice_to_have_skills == ["docker", "kubernetes", "python"]


@pytest.mark.parametrize(
    ("description", "skill"),
    [
        ("Mehrjährige Berufserfahrung als Python Developer.", "python"),
        ("Erfahrungen mit Docker sind erforderlich.", "docker"),
        ("Erfahrung im Umgang mit Kubernetes.", "kubernetes"),
    ],
)
def test_live_ba_requirement_phrasings_are_must_have(description, skill):
    result = extract_skills("Softwareentwickler", description)

    assert result.must_have_skills == [skill]
    assert result.nice_to_have_skills == []
