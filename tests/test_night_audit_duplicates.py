"""Night audit 2026-10-27: duplicate-guard regressions from live operation.

Cases are the real collected postings (public employer/agency names and
titles only, no personal data). Before the fix, the agency re-wordings of
the applied Bx4 TECH role (Akkodis, Hays, same city) were NO_DUPLICATE_FOUND
and "e.G." / umlaut / dotted-abbreviation / branch-suffix spellings of the
same employer were different companies.
"""

from datetime import UTC, datetime

import pytest

from app.models.application_lifecycle import Application
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_duplicates import (
    VacancyView,
    assess_vacancy,
    classify_duplicate,
    company_identity,
    is_intermediary,
    verdict,
)

AT = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


def _application(app_id, company, role, *, ref, location, job_id=None, url=None):
    return Application(
        id=app_id,
        candidate_profile_id=None,
        job_id=job_id,
        company_name=company,
        role_title=role,
        location=location,
        source="bundesagentur",
        source_reference=ref,
        external_job_id=None,
        vacancy_fingerprint=None,
        application_channel=C.EMPLOYER_PORTAL,
        destination_url=url,
        employer_domains=(),
        review_package_id=None,
        current_status=S.SUBMITTED,
        submitted_at=AT,
        received_at=None,
        last_status_changed_at=AT,
        last_employer_contact_at=None,
        next_followup_at=None,
        followup_required=False,
        version=1,
        created_at=AT,
        updated_at=AT,
    )


ALD_BACKEND = _application(
    2,
    "ALD Vacuum Technologies GmbH",
    "Backend Developer (m/w/div.)",
    ref="20385-2347902-S",
    location="Hanau",
)
ALD_PROTOTYPING = _application(
    3,
    "ALD Vacuum Technologies GmbH",
    "Software Engineer (m/w/div.) im Bereich Prototyping & Data Analytics",
    ref="20385-2352935-S",
    location="Hanau",
)
BX4 = _application(
    10,
    "Bx4 TECH GmbH & Co. KG",
    "IT-Systemintegrator (m/w/d) Produktions-IT / Shopfloor",
    ref="10001-1003883456-S",
    location="Frankfurt am Main",
)
NEXARO = _application(
    13,
    "Nexaro GmbH",
    "Softwaretester:in (m/w/d)",
    ref="14671967",
    location="60489 Frankfurt am Main",
)
APPLICATIONS = [ALD_BACKEND, ALD_PROTOTYPING, BX4, NEXARO]


def _ba(ref):
    return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"


def _grade(company, title, *, url=None, location=None, job_id=None):
    vacancy = VacancyView(job_id, "bundesagentur", company, title, url, None, location)
    findings = assess_vacancy(vacancy, APPLICATIONS)
    return classify_duplicate(vacancy, findings), verdict(findings), findings


def test_ald_rewritten_title_same_reference_is_exact():
    grade, old_verdict, findings = _grade(
        "ALD Vacuum Technologies  GmbH",
        "Software Engineer (m/w/div.) Additive Manufacturing & Data Analytics",
        url=_ba("20385-2352935-S"),
        location="Hanau",
    )
    assert grade == "EXACT_DUPLICATE" and old_verdict == "ALREADY_APPLIED"
    assert findings[0].application_id == 3 and "SAME_REFERENCE" in findings[0].signals


@pytest.mark.parametrize("company", ["N.O.C Engineering GmbH", "Jobriver IT Jobbörse"])
def test_ald_agency_mirror_identical_title_is_possible(company):
    grade, old_verdict, findings = _grade(
        company,
        "Backend Developer (m/w/div.)",
        url=_ba("13552-95JQBAYRDGV766Y6-S"),
        location="Offenbach am Main",
    )
    assert grade == "POSSIBLE_DUPLICATE" and old_verdict == "POSSIBLE_DUPLICATE"
    assert findings[0].application_id == 2


@pytest.mark.parametrize(
    ("company", "title"),
    [
        (
            "Akkodis Germany Tech Experts GmbH",
            "IT-Spezialist Produktions-IT & Systemintegration (m/w/d)",
        ),
        ("Hays Professional Solutions GmbH Standort Frankfurt", "Shopfloor IT Specialist (m/w/d)"),
    ],
)
def test_agency_rewording_same_city_is_possible_duplicate(company, title):
    grade, old_verdict, findings = _grade(company, title, location="Frankfurt am Main")
    assert grade == "POSSIBLE_DUPLICATE"
    # Never a block: agency findings only ask for a human look.
    assert old_verdict == "POSSIBLE_DUPLICATE"
    [finding] = [f for f in findings if f.strength != "INFO"]
    assert finding.application_id == 10
    assert finding.signals == ("SIMILAR_ROLE_VIA_INTERMEDIARY",)


@pytest.mark.parametrize(
    ("company", "title", "location"),
    [
        # Same agency + same city, different job (shares only generic words).
        ("puro Personaldienstleistung GmbH", "IT Support Mitarbeiter (m/w/d)", "Frankfurt am Main"),
        # Agency, similar role, other city.
        (
            "Akkodis Germany Tech Experts GmbH",
            "IT-Spezialist Produktions-IT & Systemintegration (m/w/d)",
            "Kassel, Hessen",
        ),
        # Software developer roles must not match the applied software TESTER role.
        ("FERCHAU GmbH Niederlassung Frankfurt", "Software Quality Engineer (m/w/d)", "Frankfurt"),
        ("Brunel GmbH NL Frankfurt", "Senior Software Developer Frontend (w/m/d)", "Frankfurt"),
        # A direct employer with a similar title is not an agency mirror.
        ("Rheinmetall AG", "Shopfloor (BDE) IT Software Engineer (m/w/d)", "Frankfurt am Main"),
    ],
)
def test_not_duplicates(company, title, location):
    grade, old_verdict, _ = _grade(company, title, location=location)
    assert grade == "NOT_DUPLICATE" and old_verdict == "NO_DUPLICATE_FOUND"


def test_same_employer_other_role_is_never_blocked():
    grade, old_verdict, findings = _grade(
        "ALD Vacuum Technologies GmbH",
        "Prozessingenieur (m/w/div.)",
        url=_ba("20385-2343000-S"),
        location="Hanau",
    )
    assert grade == "NOT_DUPLICATE" and old_verdict == "NO_DUPLICATE_FOUND"
    assert {f.strength for f in findings} == {"INFO"}


def test_same_employer_same_role_without_identifier_is_probable():
    grade, old_verdict, _ = _grade(
        "ALD Vacuum Technologies GmbH",
        "Backend Developer (m/w/div.)",
        url="https://www.xing.com/m/someid",
        location="Hanau",
    )
    # Blocked as before (STRONG), but graded as not proven.
    assert grade == "PROBABLE_DUPLICATE" and old_verdict == "ALREADY_APPLIED"


def test_junk_row_without_company_or_title_is_insufficient_evidence():
    grade, _, findings = _grade("- - - - -", "Job-Vorschläge verbessern")
    assert findings == [] or grade == "INSUFFICIENT_EVIDENCE"
    assert _grade("", "")[0] == "INSUFFICIENT_EVIDENCE"


def test_identifier_beats_missing_company():
    grade, _, _ = _grade("", "", url=_ba("20385-2347902-S"))
    assert grade == "EXACT_DUPLICATE"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Raiffeisenbank Musterdorf e.G.", "Raiffeisenbank Musterdorf eG"),
        ("N.O.C Engineering GmbH", "NOC Engineering GmbH"),
        (
            "MS Messtechnik und Softwareentwicklung Müller GmbH",
            "MS Messtechnik und Softwareentwicklung Mueller GmbH",
        ),
        ("Bx4 TECH GmbH   Co. KG", "Bx4 TECH GmbH & Co. KG"),
    ],
)
def test_company_identity_folds_spelling_variants(left, right):
    assert company_identity(left) == company_identity(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("ALD Vacuum Technologies GmbH", "ALD Technologies GmbH"),
        ("Raiffeisenbank Musterdorf eG", "Raiffeisenbank Beispieltal eG"),
        # Codex review: a branch suffix may name a different employer.
        ("Example Standort Berlin GmbH", "Example GmbH"),
    ],
)
def test_company_identity_keeps_different_employers_apart(left, right):
    assert company_identity(left) != company_identity(right)


def test_umlaut_spelling_of_same_employer_and_role_is_strong():
    app = _application(
        11,
        "MS Messtechnik und Softwareentwicklung Müller GmbH",
        "Mitarbeiter Customer Support und Anwendungsberatung (m/w/d)",
        ref="14235780",
        location="Hanau",
    )
    vacancy = VacancyView(
        None,
        "xing",
        "MS Messtechnik und Softwareentwicklung Mueller GmbH",
        "Mitarbeiter Customer Support und Anwendungsberatung (m/w/d)",
        None,
    )
    findings = assess_vacancy(vacancy, [app])
    assert verdict(findings) == "ALREADY_APPLIED"
    assert classify_duplicate(vacancy, findings) == "PROBABLE_DUPLICATE"


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Akkodis Germany Tech Experts GmbH", True),
        ("puro Personaldienstleistung GmbH", True),
        ("Jobriver IT Jobbörse", True),
        ("Hays Professional Solutions GmbH Standort Frankfurt", True),
        ("Bx4 TECH GmbH & Co. KG", False),
        ("Raiffeisenbank Musterdorf eG", False),
        ("Deloitte GmbH", False),
    ],
)
def test_intermediary_detection(company, expected):
    assert is_intermediary(company) is expected
