"""Regression: Markdown-decorated section headers must not drop the requirements below them.

Found on a copy of the production DB: a Bundesagentur description with `**Ihr Profil**` yielded
only [git, oauth, rest]; Python/FastAPI/PostgreSQL listed under the header were lost.
"""

import pytest

from app.agents.skill_extractor import extract_skills

BODY = "\n\n- Solide Grundlagen in Python, SQL/PostgreSQL\n- Sicherer Umgang mit Git"


@pytest.mark.parametrize(
    "header",
    [
        "Ihr Profil",
        "**Ihr Profil**",
        "### Ihr Profil",
        "### **Ihr Profil**",
        "**Ihr Profil:**",
        "__Ihr Profil__",
    ],
)
def test_requirements_under_decorated_profile_header_are_extracted(header):
    result = extract_skills("Softwareentwickler", header + BODY)
    assert {"python", "sql", "postgresql", "git"} <= set(result.must_have_skills)


def test_decorated_offer_header_still_ends_the_requirements_section():
    result = extract_skills(
        "Softwareentwickler",
        "**Ihr Profil**\n\n- Git\n\n**Wir bieten**\n\n- Python Weiterbildung",
    )
    assert "python" not in result.must_have_skills


def test_long_decoration_runs_stay_linear():
    """A hostile description with a huge run of Markdown characters must not freeze extraction."""
    import time

    hostile = "*" * 200_000 + "a" + " " * 200_000 + "b"
    started = time.perf_counter()
    extract_skills("Softwareentwickler", hostile + "\n- Python")
    assert time.perf_counter() - started < 2.0


def test_closing_atx_markers_are_stripped_and_offer_header_resets_context():
    result = extract_skills(
        "Softwareentwickler",
        "### Ihr Profil ###\n\n- Git\n\n### Wir bieten ###\n\n- Python Weiterbildung",
    )
    assert "git" in result.must_have_skills
    assert "python" not in result.must_have_skills
    assert "python" in extract_skills("x", "### Ihr Profil ###" + BODY).must_have_skills
