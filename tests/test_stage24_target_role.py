"""Stage 24: the target is software development; IT support / administration
titles are never recommended, and software titles are never lost to a stray
"Support" word."""

import pytest

from app.agents.target_role import is_support_admin_term, support_admin_family
from app.models.candidate_search import CandidateSearchSettings
from app.services.job_fit_assessment import role_criterion
from app.services.search_strategy import bundesagentur_queries

EXCLUDED = [
    "IT-Support Mitarbeiter (m/w/d)",
    "1st und 2nd Level IT-Support Smallworld GIS (m/w/d)",
    "IT-Mitarbeiter (m/w/d) Service-Desk",
    "Helpdesk Mitarbeiter",
    "IT Service - Techniker/in (m/w/d)",
    "IT-Servicetechniker (m/w/d) im Außendienst",
    "IT-Administrator mit Entwicklungsaufgaben (m/w/d)",
    "Systemadministrator Windows",
    "Python Systemadministrator",
    "Microsoft 365 Administrator",
    "Active Directory Spezialist",
    "Systemintegrator (m/w/d)",
    "Technical Application Support Specialist",
    "Anwenderbetreuer (m/w/d)",
    "Mitarbeiter (m/w/d) Software-Support",
    "Support Techniker IT",
    "Second Level Support Engineer",
    "IT-Supporter (m/w/d)",
    "Anwendungsbetreuer (m/w/d)",
    "IT-Admin",
    "Datenbankadministrator",
    "IT-Support mit Softwareentwicklung",
    "IT-Support mit Python-Entwicklungsaufgaben",
    "IT-Administration (m/w/d)",
    "Systemadministration (m/w/d)",
]
KEPT = [
    "Junior Python Developer (m/w/d)",
    "Quereinsteiger als Junior Python-Entwickler (m/w/d)",
    "Softwareentwickler (m/w/d)",
    "Backend Developer Python",
    "Software Developer Support Tools",
    "Python Entwickler Service Desk Tools",
    "Anwendungsentwickler (m/w/d)",
    "Trainee Softwareentwicklung",
    "QA Automation Engineer Python",
    "Software Engineer Customer Support Platform",
    "Junior KI-Entwickler",
    "IT-Administrator / Softwareentwickler für unser Unternehmen",
    "IT-Systemadministrator / Softwareentwickler m/w/d",
    "Software Development Engineer - IT-Support Tools",
    "Full Stack Engineer - Service Desk Platform",
    "IT-Administrator / Softwareingenieur",
    "Softwareentwicklungsingenieur - Administrator Tools",
    "QA Automation Engineer Python - Microsoft 365",
    "Python Developer - Active Directory Integration",
    "Softwareentwicklungsingenieur (m/w/d) - Service Desk Plattform",
    "Fachinformatiker Anwendungsentwicklung (m/w/d) - Microsoft 365",
]


@pytest.mark.parametrize("title", EXCLUDED)
def test_support_and_admin_titles_are_flagged(title):
    assert support_admin_family(title) is not None


@pytest.mark.parametrize("title", KEPT)
def test_software_titles_are_never_flagged(title):
    assert support_admin_family(title) is None


def test_stray_support_word_in_description_is_irrelevant():
    # only titles are inspected
    assert support_admin_family("Python Developer") is None


def _settings(**kw):
    return CandidateSearchSettings(
        primary_roles=["Junior Python Developer"],
        secondary_roles=["Application Support / Technical Application Support"],
        **kw,
    )


@pytest.mark.parametrize("title", EXCLUDED)
def test_role_is_off_role_even_with_stale_support_role_configured(title):
    criterion = role_criterion(title, _settings())
    assert criterion.status == "OFF_ROLE"
    assert "software development" in criterion.reason


@pytest.mark.parametrize("title", ["Junior Python Developer (m/w/d)", "Softwareentwickler"])
def test_software_titles_keep_their_role_status(title):
    assert role_criterion(title, _settings()).status != "OFF_ROLE"


def test_support_search_keywords_are_not_searched():
    assert is_support_admin_term("Application Support")
    assert is_support_admin_term("Systemintegrator")
    assert not is_support_admin_term("Softwaretester")
    assert not is_support_admin_term("Python")


class _S:
    bundesagentur_search_keywords = "Python"
    bundesagentur_search_location = "Frankfurt"
    bundesagentur_search_radius_km = 50


def test_queries_drop_support_keywords():
    search = CandidateSearchSettings(
        home_city="Heusenstamm",
        home_postal_code="63150",
        commute_origin_label="Heusenstamm Bahnhof",
        max_commute_km=100,
        commute_measure="CAR_ROUTE",
        search_keywords=["Python", "Application Support", "Softwaretester", "Systemintegrator"],
    )
    terms = {q.keywords for q in bundesagentur_queries(_S(), search)}
    assert terms == {"Python", "Softwaretester"}


def test_all_keywords_support_falls_back_to_default_term():
    search = CandidateSearchSettings(search_keywords=["Application Support"])
    assert [q.keywords for q in bundesagentur_queries(_S(), search)] == ["Python"]


def test_all_support_keywords_and_no_default_term_gives_no_query():
    class _Empty(_S):
        bundesagentur_search_keywords = ""

    search = CandidateSearchSettings(search_keywords=["Application Support"])
    assert bundesagentur_queries(_Empty(), search) == []


def test_default_keyword_that_is_support_is_not_searched():
    class _Support(_S):
        bundesagentur_search_keywords = "IT-Support"

    search = CandidateSearchSettings(search_keywords=["Application Support"])
    assert bundesagentur_queries(_Support(), search) == []


def test_no_search_stored_default_support_term_is_not_searched():
    class _Support(_S):
        bundesagentur_search_keywords = "IT-Support"

    assert bundesagentur_queries(_Support(), None) == []


def test_nothing_stored_and_blank_default_keeps_legacy_single_query():
    class _Blank(_S):
        bundesagentur_search_keywords = ""

    assert [q.keywords for q in bundesagentur_queries(_Blank(), None)] == [""]


def test_no_search_stored_target_default_is_kept():
    assert [q.keywords for q in bundesagentur_queries(_S(), None)] == ["Python"]


@pytest.mark.parametrize(
    "title",
    [
        "Fachinformatiker (m/w/d) für Anwendungsentwicklung - Service Desk Tools",
        "Fachinformatiker/in für Anwendungsentwicklung - IT-Support Tools",
        "Fachinformatiker / in fuer Anwendungsentwicklung - Helpdesk Tools",
    ],
)
def test_fachinformatiker_anwendungsentwicklung_gender_forms_are_exempt(title):
    assert support_admin_family(title) is None


def test_long_unmatched_token_is_linear_time():
    import time

    started = time.perf_counter()
    support_admin_family("x" * 32000)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "title",
    [
        "Softwareentwicklungsingenieur - Administrator Tools",
        "IT-Administrator / Softwareentwickler für unser Unternehmen",
        "QA Automation Engineer Python - Microsoft 365",
    ],
)
def test_developer_hybrids_are_not_off_role_in_the_role_criterion(title):
    assert role_criterion(title, _settings()).status != "OFF_ROLE"


@pytest.mark.parametrize(
    "title",
    [
        "QA Automation Engineer Python - Service Desk Platform",
        "Test Automation Engineer - Helpdesk-Portal",
        "Softwaretester (m/w/d) IT-Support Software",
    ],
)
def test_software_qa_roles_are_kept(title):
    assert support_admin_family(title) is None


@pytest.mark.parametrize(
    "title", ["IT-Support mit Testautomatisierung", "Systemadministrator mit Testautomatisierung"]
)
def test_incidental_testing_tasks_do_not_exempt_support(title):
    assert support_admin_family(title) is not None


def test_testautomatisierer_role_noun_is_kept():
    assert support_admin_family("Testautomatisierer (m/w/d) - Service-Desk-Software") is None


@pytest.mark.parametrize(
    "title",
    [
        "Softwarearchitekt (m/w/d) - Service-Desk-Plattform",
        "Software Architect IT-Support Platform",
    ],
)
def test_software_architects_are_kept(title):
    assert support_admin_family(title) is None
