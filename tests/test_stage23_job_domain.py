"""Stage 23: specialization detection (regression fixtures from the Stage 22
false matches: SAP, Salesforce, Embedded, PLC, PKI, Sales)."""

import pytest

from app.agents.job_domain import extract_domain_profile, title_domain
from app.services.job_fit_assessment import domain_criterion


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Embedded Linux Softwareentwickler (m/w/d)", "EMBEDDED"),
        ("Embedded Software Entwickler:in (m/w/d)", "EMBEDDED"),
        ("SAP Consultant (m/w/d) Nachhaltigkeit diskrete Fertigung", "SAP"),
        ("SAP-Entwickler ABAP", "SAP"),
        ("Global Salesforce Data Cloud Specialist (m/f/d)", "SALESFORCE"),
        ("PKI & Certificate Management Engineer (m/f/d)", "PKI"),
        ("SPS-Programmierer (m/w/d)", "PLC"),
        ("Junior Sales Manager – Home Care & Industrial Applications", "SALES"),
        ("Business Development Manager", "SALES"),
    ],
)
def test_title_names_off_target_domain(title, domain):
    assert title_domain(title)[0] == domain


@pytest.mark.parametrize(
    "title",
    [
        "Junior Python Backend Developer (m/w/d)",
        "Python Entwickler SAP-Integration",
        "Fullstack Softwareentwickler Python",
        "IT Support Specialist Salesforce",
        "Sales Engineer Python",
        "Fachinformatiker Anwendungsentwicklung",
        "Quereinsteiger IT",
    ],
)
def test_target_or_generic_titles_are_not_off_target(title):
    assert title_domain(title) is None


def test_mentions_are_graded_not_blindly_required():
    text = (
        "Das bringst du mit:\n- Erfahrung mit Python und FastAPI\n"
        "Nice to have:\n- SAP-Kenntnisse\n"
        "Wir nutzen Embedded-Geräte unserer Kunden."
    )
    profile = extract_domain_profile("Backend Entwickler", text)
    assert {m.term for m in profile.required("TARGET_STACK")} >= {"python", "fastapi"}
    assert profile.required("SAP") == []
    assert profile.required("EMBEDDED") == []
    sap = [m for m in profile.mentions if m.term == "sap"]
    assert sap and sap[0].importance == "PREFERRED"


def test_off_domain_requires_literal_evidence():
    ok = domain_criterion("Softwareentwickler (m/w/d)", "Wir freuen uns auf deine Bewerbung.")
    assert ok.status == "OK"  # no text is never a mismatch
    preferred = domain_criterion(
        "IT Developer", "Das bringst du mit:\n- Python\nNice to have:\n- SAP ABAP"
    )
    assert preferred.status == "OK"


def test_title_domain_is_off_domain():
    result = domain_criterion("Embedded Linux Softwareentwickler", "Linux, C, Git")
    assert result.status == "OFF_DOMAIN"
    assert "EMBEDDED" in result.reason


def test_required_off_target_without_target_stack_is_off_domain():
    text = "Das bringst du mit:\n- Fundierte Kenntnisse in SAP ABAP\n- Teamfähigkeit"
    assert domain_criterion("IT Spezialist (m/w/d)", text).status == "OFF_DOMAIN_REQUIRED"


def test_required_off_target_with_required_python_stays_ok():
    text = "Das bringst du mit:\n- Kenntnisse in Python\n- Kenntnisse in SAP"
    assert domain_criterion("IT Spezialist (m/w/d)", text).status == "OK"


def test_off_target_trainee_programme_goes_to_operator_not_skip():
    result = domain_criterion("Trainee SAP Software Developer (all genders)", "")
    assert result.status == "OFF_DOMAIN_TRAINING"
    result = domain_criterion("Quereinsteiger gesucht: SPS-Steuerung (m/w/d)", "")
    assert result.status == "OFF_DOMAIN_TRAINING"


@pytest.mark.parametrize(
    "title",
    [
        "Junior Back End Developer SAP-Integration",
        "Junior SAP Anwendungsbetreuer (m/w/d)",
        "Junior Salesforce 1st Level IT-Mitarbeiter (m/w/d)",
        "Junior QA Engineer Embedded Systems",
        "Junior Selenium Engineer SAP",
    ],
)
def test_target_role_words_shield_the_title(title):
    assert title_domain(title) is None


def test_required_python_stack_beats_off_target_title():
    text = "Das bringst du mit:\n- Kenntnisse in Python und FastAPI"
    result = domain_criterion("Junior SAP Integration Developer", text)
    assert result.status == "OFF_DOMAIN_REQUIRED"


def test_python_under_english_requirements_heading_still_protects():
    text = "Requirements:\n- Python\n- FastAPI"
    result = domain_criterion("Junior SAP Integration Developer", text)
    assert result.status == "OFF_DOMAIN_REQUIRED"


@pytest.mark.parametrize(
    "title", ["Junior Quality Assurance Engineer SAP", "Junior SAP Softwaretester (m/w/d)"]
)
def test_qa_aliases_shield_the_title(title):
    assert title_domain(title) is None


def test_optional_repeat_does_not_hide_a_plain_mention():
    text = "Requirements:\n- Python\nNice to have:\n- Python experience"
    result = domain_criterion("Junior SAP Integration Developer", text)
    assert result.status == "OFF_DOMAIN_REQUIRED"


def test_configured_specialization_role_is_respected():
    result = domain_criterion("Junior SAP Consultant", "", ("SAP Consultant",))
    assert result.status == "OK"
    assert domain_criterion("Junior SAP Consultant", "", ("Python Developer",)).status == (
        "OFF_DOMAIN"
    )


def test_configured_role_alternatives_are_split():
    roles = ("Python Developer / SAP Consultant",)
    assert domain_criterion("Junior SAP Consultant", "", roles).status == "OK"


def test_software_role_for_sales_software_is_not_a_sales_job():
    assert title_domain("Junior Softwareentwickler fuer Vertriebssoftware") is None
    assert title_domain("Junior Sales Manager") is not None


def test_software_sales_manager_is_still_sales():
    assert title_domain("Junior Software Sales Manager")[0] == "SALES"


def test_hyphenated_service_desk_title_is_support():
    assert title_domain("Junior SAP Service-Desk-Agent") is None
    assert title_domain("Junior SAP Help-Desk Mitarbeiter") is None


@pytest.mark.parametrize(
    "title",
    [
        "Analyst Vertriebsperformance & BI Reporting (m/w/d)",
        "Data & Tracking Solution Architect - Customer Sales (m/w/d)",
        "Sales Data Analyst (m/w/d)",
    ],
)
def test_data_and_analyst_roles_are_not_sales_specializations(title):
    assert title_domain(title) is None


def test_plain_sales_titles_stay_sales():
    for title in ("Junior Sales Manager", "Business Development Manager", "Vertriebsmitarbeiter"):
        assert title_domain(title)[0] == "SALES"
