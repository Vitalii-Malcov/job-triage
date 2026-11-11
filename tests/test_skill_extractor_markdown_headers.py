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
