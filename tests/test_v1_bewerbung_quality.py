"""Production V1 quality patch: the Bewerbung package an operator reviews
(and the CV document an employer would receive) must contain only
candidate-facing facts, in clean German, with the dates the profile really
records -- and the cover letter must connect the vacancy's actual
responsibilities with verified candidate evidence.

The fixture mirrors the SHAPE of the real first-run profile (English
responsibility text, period stored only in the description, internal
provenance notes, UNKNOWN proficiency enums) with a generic data-analyst
vacancy. Nothing here invents candidate facts.
"""

import asyncio
from datetime import UTC, datetime

from app.agents.bewerbung_renderer import build_evidence, parse_plan, render_draft, resolve_plan
from app.agents.cv_document import build_cv_document
from app.models.bewerbung import (
    AllowedClaim,
    BewerbungDraft,
    BewerbungJobContext,
    BewerbungParagraph,
    BewerbungProviderPlan,
)
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.cv_draft import (
    CVEducationItem,
    CVExperienceItem,
    CVHeader,
    CVLanguageItem,
    CVProjectItem,
    CVSkillItem,
    CVTopLevelFact,
    TailoredCVDraft,
)
from app.models.review_package import (
    ReviewedBewerbungContent,
    ReviewedBewerbungParagraph,
    ReviewedCVContent,
    ReviewSectionOrderField,
    ReviewTextField,
)
from app.providers.bewerbung.deterministic import DeterministicBewerbungProvider
from app.services.telegram_bewerbung_approval import ReviewSnapshot, render_review_document
from app.services.telegram_bewerbung_preview import render_summary
from app.services.telegram_operator_render import (
    MatchSummary,
    OperatorCardView,
    PackageSummary,
    render_card,
    render_details,
)

JOB_TITLE = "Datenanalyst (m/w/d)"
JOB_COMPANY = "Beispiel Krankenkasse"
JOB_DESCRIPTION = (
    "Ihre Aufgaben: Analyse und Visualisierung von Daten, Erstellung von Reports, "
    "Dashboards und KPIs, Weiterentwicklung des Data-Warehouse und des Reportings, "
    "Sicherstellung der Datenqualität. Ihr Profil: abgeschlossenes Studium der "
    "Informatik oder vergleichbar, sehr gute Kenntnisse in SQL und Python, "
    "Erfahrung mit BI-Werkzeugen. Gute Deutschkenntnisse."
)

SECTION_ORDER = [
    "HEADER",
    "SUMMARY",
    "SKILLS",
    "EXPERIENCE",
    "PROJECTS",
    "EDUCATION",
    "CERTIFICATIONS",
    "LANGUAGES",
]

FORBIDDEN_EMPLOYER_FACING = (
    "Confirmed by candidate",
    "confirmed by candidate",
    "Not software-related",
    "Period per Lebenslauf",
    "per candidate statement",
    "Corrected by candidate",
    "month/year only",
    "years only",
    "UNKNOWN",
    "LANGUAGE",
    "DATABASE",
    "Kategorie:",
    "provenance",
    "verified_at",
    "confidence",
)

ENGLISH_PROFILE_TEXT = (
    "Technical support",
    "Error analysis and troubleshooting",
    "Decision-making under pressure",
    "Team leadership and coordination",
    "Own Python project",
    "Processing structured data",
)


def _fact(value: str, field: str) -> CVTopLevelFact:
    return CVTopLevelFact(value=value, source_id=1, source_field=field, profile_version=4)


def _skill(source_id: int, text: str, category: str, requirement: str) -> CVSkillItem:
    return CVSkillItem(
        text=text,
        category=category,
        proficiency="UNKNOWN",
        years_experience=None,
        source_id=source_id,
        match_requirement=requirement,
        importance="REQUIRED",
    )


def _experience(source_id: int, **values) -> CVExperienceItem:
    data = dict(
        source_id=source_id,
        start_date=None,
        end_date=None,
        is_current=False,
        location=None,
        description=None,
    )
    data.update(values)
    return CVExperienceItem(**data)


def _project(source_id: int, name: str, description: str, technologies, highlights, matched):
    return CVProjectItem(
        source_id=source_id,
        name=name,
        description=description,
        role=None,
        technologies=technologies,
        repository_url=None,
        demo_url=None,
        start_date=None,
        end_date=None,
        highlights=highlights,
        matched_skills=matched,
    )


def _language(source_id: int, language: str, level: str) -> CVLanguageItem:
    return CVLanguageItem(source_id=source_id, language=language, level=level, certificate=None)


def _cv() -> TailoredCVDraft:
    return TailoredCVDraft(
        id=1,
        created_at=datetime.now(UTC),
        job_id=212,
        match_id=376,
        candidate_profile_version=4,
        match_algorithm_version="v12",
        cv_adapter_version="v2",
        header=CVHeader(
            first_name=_fact("Max", "first_name"),
            last_name=_fact("Muster", "last_name"),
            professional_title=_fact("Junior Python Backend Developer", "professional_title"),
            location_city=_fact("Frankfurt am Main", "location_city"),
            location_country=_fact("Germany", "location_country"),
        ),
        professional_summary=_fact(
            "Junior Python Backend Developer in Weiterbildung.", "professional_summary"
        ),
        section_order=SECTION_ORDER,
        skills=[
            _skill(9, "Python", "LANGUAGE", "python"),
            _skill(12, "SQL", "DATABASE", "sql"),
        ],
        experience=[
            _experience(
                1,
                company="IT-Dienstleister GmbH",
                job_title="IT-Mitarbeiter",
                is_current=True,
                location="Deutschland",
                description=(
                    "Teilzeit / Minijob. Period per Lebenslauf: 02/2023 – current "
                    "(month/year only)."
                ),
                responsibilities=[
                    "Technical support",
                    "Error analysis and troubleshooting in ongoing IT operations",
                    "Ongoing system support",
                ],
            ),
            _experience(
                2,
                company="Grenzpolizei",
                job_title="Leitende Positionen – Grenzpolizei",
                location="Republik Moldau",
                description="Period per Lebenslauf: 2006–2021 (years only).",
                responsibilities=[
                    "Team leadership and coordination",
                    "Decision-making under pressure",
                    "Structured reporting",
                ],
            ),
            _experience(
                3,
                company="Baugesellschaft GmbH",
                job_title="Hilfsarbeiter (Bauhelfer)",
                location="Butzbach, Deutschland",
                description=(
                    "Period per Lebenslauf: 01/2022 – 08/2023 (month/year only). "
                    "Confirmed by candidate 2026-10-20. Not software-related."
                ),
                responsibilities=["Unterstützung bei allgemeinen Bauarbeiten"],
            ),
        ],
        projects=[
            _project(
                1,
                "Movie Search App – Flask Webprojekt",
                "Own Python backend project: web application for movie search.",
                ["Python", "Flask", "MySQL", "SQL"],
                ["Web application", "Search-query logging", "Statistics page"],
                ["Python", "SQL"],
            ),
            _project(
                2,
                "Automatisierung & Datenverarbeitung",
                "Own exercise projects in Python.",
                ["Python"],
                [
                    "File processing",
                    "Processing structured data",
                    "Analysis/evaluation of structured data",
                ],
                ["Python"],
            ),
            _project(
                3,
                "Stock Momentum Screener",
                "Own Python project: CLI tool for analysis of stock momentum.",
                ["Python"],
                ["CLI tool for analysis of stock momentum", "HTML reporting"],
                ["Python"],
            ),
        ],
        education=[
            CVEducationItem(
                source_id=3,
                institution="Informatikschule",
                program="Ausbildung im Bereich Programmierung",
                degree=None,
                field_of_study=None,
                start_date=None,
                end_date=None,
                completed=False,
                location=None,
            )
        ],
        languages=[
            _language(1, "Deutsch", "B1"),
            _language(2, "Englisch", "UNKNOWN"),
            _language(3, "Russisch", "NATIVE"),
        ],
    )


def _req(name: str, status: str, kind: str = "SKILL") -> RequirementMatch:
    return RequirementMatch(
        requirement=name,
        normalized_requirement=name.lower(),
        requirement_type=kind,
        importance="REQUIRED",
        match_status=status,
        reason="fixture",
    )


def _match() -> CandidateJobMatch:
    return CandidateJobMatch(
        id=376,
        created_at=datetime.now(UTC),
        job_id=212,
        candidate_profile_version=4,
        company_research_id=None,
        algorithm_version="v12",
        overall_score=100,
        coverage_score=100,
        required_skill_score=100,
        preferred_skill_score=100,
        experience_support_score=100,
        matched_requirements=[_req("Python", "MATCH"), _req("SQL", "MATCH")],
        unknown_requirements=[
            _req("Completed degree (Informatik or comparable)", "UNKNOWN", "EDUCATION")
        ],
    )


def _letter_draft() -> BewerbungDraft:
    return BewerbungDraft(
        id=1,
        created_at=datetime.now(UTC),
        job_id=212,
        cv_draft_id=1,
        match_id=376,
        candidate_profile_version=4,
        job_snapshot_fingerprint="f" * 64,
        match_algorithm_version="v12",
        cv_adapter_version="v2",
        bewerbung_generator_version="v4",
        provider="deterministic",
        subject=f"Bewerbung als {JOB_TITLE}",
        salutation="Sehr geehrte Damen und Herren,",
        opening="mit großem Interesse habe ich Ihre Stellenanzeige gelesen.",
        body_paragraphs=[
            BewerbungParagraph(text="Ich bringe Kenntnisse in Python mit.", source_claim_ids=[])
        ],
        closing="Für Rückfragen stehe ich Ihnen gerne zur Verfügung.",
        signature_name="Max Muster",
        plan=BewerbungProviderPlan(
            opening_style="ROLE_INTEREST",
            paragraphs=[{"kind": "GENERIC", "claim_ids": []}],
            closing_style="INTERVIEW_INTEREST",
        ),
        claims=[
            AllowedClaim(
                id="candidate_skill:9",
                claim="Python",
                source_entity="candidate_skill",
                source_id=9,
            )
        ],
        job_context=BewerbungJobContext(title=JOB_TITLE, company=JOB_COMPANY),
    )


def _review_text(value: str | None) -> ReviewTextField:
    return ReviewTextField(value=value)


def _snapshot() -> ReviewSnapshot:
    cv = _cv()
    return ReviewSnapshot(
        reviewed_cv=ReviewedCVContent(
            professional_title=_review_text(cv.header.professional_title.value),
            professional_summary=_review_text(cv.professional_summary.value),
            section_order=ReviewSectionOrderField(value=SECTION_ORDER),
        ),
        reviewed_bewerbung=ReviewedBewerbungContent(
            subject=_review_text(f"Bewerbung als {JOB_TITLE}"),
            salutation=_review_text("Sehr geehrte Damen und Herren,"),
            opening=_review_text("mit großem Interesse habe ich Ihre Stellenanzeige gelesen."),
            body_paragraphs=[ReviewedBewerbungParagraph(text="Absatz.")],
            closing=_review_text("Für Rückfragen stehe ich Ihnen gerne zur Verfügung."),
            signature_name=_review_text("Max Muster"),
        ),
        cv=cv,
        match=_match(),
        job_title=JOB_TITLE,
        job_company=JOB_COMPANY,
    )


def _cv_document_text() -> str:
    return "\n".join(block.text for block in build_cv_document(_cv(), None).blocks)


def _review_document_text() -> str:
    document = render_review_document(_snapshot())
    assert document.decidable
    return "\n".join(document.bodies)


def _review_cv_section() -> str:
    text = _review_document_text()
    return text.split("LEBENSLAUF", 1)[1].split("Hinweise:", 1)[0]


def _generated_letter():
    cv, match = _cv(), _match()
    packet, registry = build_evidence(cv, match, JOB_TITLE, JOB_COMPANY, JOB_DESCRIPTION)
    plan = parse_plan(asyncio.run(DeterministicBewerbungProvider().generate_plan(packet)))
    rendered = render_draft(
        plan,
        resolve_plan(plan, registry),
        JOB_TITLE,
        JOB_COMPANY,
        "Junior Python Backend Developer",
        job_description=JOB_DESCRIPTION,
    )
    return rendered


def _letter_text(rendered) -> str:
    return "\n\n".join(
        [rendered.opening, *(p.text for p in rendered.body_paragraphs), rendered.closing]
    )


# --- B: candidate-match display ---------------------------------------------------


class TestPerfectMatchDisplay:
    def test_preview_does_not_present_open_requirements_as_perfect_fit(self):
        text = render_summary(_letter_draft(), _cv(), _match())
        assert "Bestätigte Profil-Nachweise: 2/3 Anforderungen" in text
        assert "Offene Anforderungen: 1" in text
        assert "kein vollständiger Stellen-Fit" in text
        for line in text.split("\n"):
            assert not line.rstrip().endswith("100/100"), line

    def test_review_document_does_not_present_open_requirements_as_perfect_fit(self):
        text = _review_document_text()
        assert "Bestätigte Profil-Nachweise: 2/3 Anforderungen" in text
        assert "Offene Anforderungen: 1" in text
        for line in text.split("\n"):
            assert not line.rstrip().endswith("100/100"), line

    def test_fully_evidenced_match_keeps_plain_score_line(self):
        match = _match().model_copy(update={"unknown_requirements": []})
        text = render_summary(_letter_draft(), _cv(), match)
        assert "Kandidaten-Match (belegte Profil-Nachweise): 100/100" in text
        assert "Offene Anforderungen" not in text

    def test_operator_card_and_details_flag_open_requirements(self):
        view = OperatorCardView(
            application_id=1,
            company=JOB_COMPANY,
            role=JOB_TITLE,
            location=None,
            source="bundesagentur",
            reference=None,
            channel="EMAIL",
            status="PACKAGE_READY",
            package=PackageSummary(ready=True),
            match=MatchSummary(
                overall=100,
                required=100,
                preferred=100,
                experience=100,
                matched=("Python", "SQL"),
                unknown_count=1,
            ),
        )
        now = datetime.now(UTC)
        for text in (render_card(view, now=now), render_details(view, now=now)):
            assert "Open requirements: 1" in text
            assert "not a full fit" in text


# --- B: CV sanitization / German rendering ---------------------------------------


class TestEmployerFacingCV:
    def test_cv_document_contains_no_internal_metadata_or_enums(self):
        text = _cv_document_text()
        for forbidden in FORBIDDEN_EMPLOYER_FACING:
            assert forbidden not in text, forbidden

    def test_review_cv_section_contains_no_internal_metadata_or_enums(self):
        text = _review_cv_section()
        for forbidden in FORBIDDEN_EMPLOYER_FACING:
            assert forbidden not in text, forbidden
        assert "Niveau: UNKNOWN" not in text

    def test_known_periods_render_instead_of_missing(self):
        for text in (_cv_document_text(), _review_cv_section()):
            assert "02/2023 – heute" in text
            assert "2006 – 2021" in text
            assert "01/2022 – 08/2023" in text
        assert "Zeitraum: nicht angegeben" not in _review_cv_section().split("Projekte:")[0]

    def test_public_description_text_is_kept(self):
        for text in (_cv_document_text(), _review_cv_section()):
            assert "Teilzeit / Minijob" in text

    def test_german_cv_has_no_raw_english_profile_text(self):
        for text in (_cv_document_text(), _review_cv_section()):
            for english in ENGLISH_PROFILE_TEXT:
                assert english not in text, english
            assert "Technischer Support" in text
            assert "Entscheidungsfindung unter Druck" in text
            assert "Verarbeitung strukturierter Daten" in text
            assert "Germany" not in text

    def test_skills_render_naturally(self):
        text = _review_cv_section()
        assert "Python" in text and "SQL" in text
        assert "Niveau:" not in text.split("Berufserfahrung:")[0]

    def test_unknown_language_level_is_not_shown_as_enum(self):
        for text in (_cv_document_text(), _review_cv_section()):
            assert "Englisch" in text
            assert "Niveau nicht angegeben" not in text
            assert "Deutsch – B1" in text or "Deutsch: B1" in text

    def test_unfinished_education_is_never_rendered_as_degree(self):
        text = _cv_document_text()
        assert "nicht abgeschlossen" in text


# --- B: cover letter relevance -----------------------------------------------------


class TestCoverLetterRelevance:
    def test_letter_connects_vacancy_responsibilities_with_verified_evidence(self):
        text = _letter_text(_generated_letter())
        assert "Python" in text and "SQL" in text
        assert "Datenanalyse" in text
        assert "Reporting" in text
        assert "strukturierter Daten" in text
        for project in (
            "Movie Search App",
            "Automatisierung & Datenverarbeitung",
            "Stock Momentum Screener",
        ):
            assert project in text, project

    def test_letter_has_several_meaningful_paragraphs(self):
        rendered = _generated_letter()
        assert 3 <= len(rendered.body_paragraphs) <= 5

    def test_letter_makes_no_unverified_claims(self):
        text = _letter_text(_generated_letter())
        for claim in (
            "Dashboard",
            "Business Intelligence",
            "BI-",
            "Krankenkasse",
            "Gesundheitswesen",
            "Studium",
            "Informatik",
            "Abschluss",
            "abgeschlossen",
        ):
            assert claim not in text.replace(JOB_COMPANY, ""), claim

    def test_letter_is_german_only(self):
        text = _letter_text(_generated_letter())
        for english in ENGLISH_PROFILE_TEXT:
            assert english not in text, english

    def test_letter_still_cites_only_verified_language_level(self):
        text = _letter_text(_generated_letter())
        assert "Deutschkenntnisse auf B1-Niveau" in text
        assert "B2" not in text
