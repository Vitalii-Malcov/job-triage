"""Stage 14D: live-pilot readiness system blockers 1-5.

Canonical runtime targeting, application contact data, local CV/letter DOCX
files + attachment plan, stable vacancy identity for research artifacts,
clean CLI failures, and vacancy-relevant language evidence. All contact
data here is synthetic. Nothing in this file sends anything.
"""

import ast
import json
import os
import sqlite3
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.document_export as export
import scripts.stage14a_send_readiness as cli
import scripts.stage14d_canonical_runtime as runtime_cli
from app.agents.bewerbung_renderer import build_evidence
from app.agents.cv_document import build_cv_document
from app.agents.language_relevance import language_key, posting_language, relevant_languages
from app.core import canonical_runtime
from app.core.canonical_runtime import (
    CanonicalRuntimeError,
    canonical_environment,
    canonical_settings,
    run_preflight,
)
from app.core.startup_diagnostics import SAFETY_FLAGS
from app.db.base import Base
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.models import JobRecord
from app.models.application_destination import (
    ApplicationDestination,
    VacancyIdentity,
)
from app.models.application_identity import (
    CONTACT_DATA_MISSING,
    CONTACT_NAME_MISMATCH,
    CONTACT_PHONE_MISSING,
    CONTACT_PROFILE_NAME_UNTRUSTED,
    ApplicationIdentity,
    ApplicationIdentityInvalidError,
    contact_problems_for_names,
    load_application_identity,
)
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.candidate_profile import CandidateLanguage, CandidateProfilePatchRequest
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
    ReviewTextField,
)
from app.services.readiness import expected_alembic_heads
from app.services.send_readiness import (
    DESTINATION_VACANCY_MISMATCH,
    LOCATION_EVIDENCE_VACANCY_MISMATCH,
    LocationEvidence,
    VacancyEvidenceMismatchError,
    destination_matches_job,
    load_send_readiness_inputs,
)

_NOW = datetime(2026, 10, 16, tzinfo=UTC)
_REPO = Path(__file__).resolve().parents[1]
_E362_FINGERPRINT = "72d52b51fd59fd7ef92df4a6e89b963b96cbe0504346cbbb3196c6a10025d2a7"
_E362_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/20079-GR6IFL6L-S"
_GERMAN_AD = (
    "Für die Organisationseinheit suchen wir zum nächstmöglichen Termin einen Entwickler. "
    "Wir entwickeln und betreiben Lösungen für interne Prozesse und arbeiten gemeinsam in "
    "Projekten mit. Ihre Aufgaben sind die Entwicklung von Anwendungen mit Python, die "
    "Integration von Schnittstellen und die Dokumentation. Sie bringen Erfahrung mit und "
    "arbeiten gerne im Team. Wir bieten Ihnen ein Umfeld, in dem Sie sich entfalten können, "
    "und unterstützen Sie bei der Weiterbildung. Der Standort ist in Deutschland."
)
_ENGLISH_AD = (
    "We are looking for a developer to join our team. You will build and run services for "
    "our internal processes and work with the product team on new features. You have "
    "experience with Python and you are comfortable with testing. We offer a friendly team, "
    "a modern stack and the chance to grow with us. Our office is in the city and we are "
    "happy to support remote work for the right person."
)


def _fact(value: str, field: str) -> CVTopLevelFact:
    return CVTopLevelFact(value=value, source_id=1, source_field=field, profile_version=1)


def _cv(*, languages=None, education=None, header=None) -> TailoredCVDraft:
    return TailoredCVDraft(
        id=1,
        created_at=_NOW,
        job_id=1,
        match_id=1,
        candidate_profile_version=1,
        match_algorithm_version="v12",
        cv_adapter_version="v2",
        header=header
        or CVHeader(
            first_name=_fact("Erika", "first_name"),
            last_name=_fact("Mustermann", "last_name"),
            professional_title=_fact("Junior Python Developer", "professional_title"),
            location_city=_fact("Hannover", "location_city"),
        ),
        professional_summary=_fact("Synthetische Zusammenfassung.", "professional_summary"),
        section_order=[
            "HEADER",
            "SUMMARY",
            "SKILLS",
            "PROJECTS",
            "EXPERIENCE",
            "EDUCATION",
            "CERTIFICATIONS",
            "LANGUAGES",
        ],
        skills=[
            CVSkillItem(
                text="Python",
                category="LANGUAGE",
                proficiency="UNKNOWN",
                years_experience=None,
                source_id=1,
                match_requirement="python",
                importance="REQUIRED",
            )
        ],
        experience=[
            CVExperienceItem(
                source_id=2,
                company="Beispiel GmbH",
                job_title="IT-Support",
                start_date=None,
                end_date=None,
                is_current=True,
                location=None,
                description="Teilzeit.",
                responsibilities=["Support <intern> & Wartung"],
            )
        ],
        projects=[
            CVProjectItem(
                source_id=3,
                name="Demo App",
                description=None,
                role=None,
                technologies=["Python"],
                repository_url=None,
                demo_url=None,
                start_date=date(2025, 3, 1),
                end_date=date(2025, 6, 1),
                highlights=["Suche"],
            )
        ],
        education=education
        if education is not None
        else [
            CVEducationItem(
                source_id=4,
                institution="Beispiel-Akademie",
                program="Python Backend",
                degree=None,
                field_of_study=None,
                start_date=None,
                end_date=None,
                completed=False,
                location=None,
            )
        ],
        languages=languages
        if languages is not None
        else [
            CVLanguageItem(source_id=5, language="Deutsch", level="B1", certificate=None),
            CVLanguageItem(source_id=6, language="Englisch", level="UNKNOWN", certificate=None),
        ],
    )


def _identity(**overrides) -> ApplicationIdentity:
    data = dict(
        full_name="Erika Mustermann",
        email="erika.mustermann@example.org",
        phone="+49 511 1234567",
        city="Hannover",
        postal_code="30159",
        confirmed_by="OPERATOR",
        confirmed_at=_NOW,
    )
    data.update(overrides)
    return ApplicationIdentity(**data)


def _match(requirements=()) -> CandidateJobMatch:
    return CandidateJobMatch(
        id=1,
        created_at=_NOW,
        job_id=1,
        candidate_profile_version=1,
        company_research_id=None,
        algorithm_version="v12",
        overall_score=50,
        coverage_score=50,
        required_skill_score=50,
        preferred_skill_score=100,
        experience_support_score=0,
        job_snapshot_fingerprint=_E362_FINGERPRINT,
        matched_requirements=list(requirements),
    )


def _all_text(blocks) -> str:
    return "\n".join(block.text for block in blocks)


# --- 1. canonical runtime ------------------------------------------------------


@pytest.fixture()
def canonical_db(tmp_path, monkeypatch):
    """A local SQLite file at the application's migration head; the working
    directory moves to tmp_path so the operator's real .env is never read."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "canonical.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", expected_alembic_heads())
    return path


def test_canonical_environment_selects_the_file_and_disables_every_safety_flag(tmp_path):
    env = canonical_environment(tmp_path / "x.db")
    assert env["DATABASE_URL"] == f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
    assert env["POSTGRES_HOST"] == ""
    for flag in SAFETY_FLAGS:
        assert env[flag.upper()] == "false"
    assert {"OUTBOUND_SENDING_ENABLED", "ALEMBIC_AUTO_UPGRADE"} <= set(env)


def test_canonical_settings_override_hostile_environment_and_restore_it(canonical_db, monkeypatch):
    monkeypatch.setenv("OUTBOUND_SENDING_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_GMAIL_DRAFT_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./stage11_pilot.db")
    settings = canonical_settings(canonical_db)
    assert settings.database_url == f"sqlite:///{canonical_db.as_posix()}"
    assert not settings.outbound_sending_enabled
    assert not settings.telegram_gmail_draft_enabled
    assert not settings.alembic_auto_upgrade
    # the process environment is restored afterwards
    assert os.environ["OUTBOUND_SENDING_ENABLED"] == "true"
    assert os.environ["DATABASE_URL"] == "sqlite:///./stage11_pilot.db"


@pytest.mark.parametrize("name", ["missing.db", "not-a-db.txt"])
def test_canonical_runtime_fails_closed_for_missing_or_wrong_file(tmp_path, name):
    if name.endswith(".txt"):
        (tmp_path / name).write_text("x")
    with pytest.raises(CanonicalRuntimeError):
        canonical_settings(tmp_path / name)


def test_canonical_runtime_refuses_a_relative_path():
    with pytest.raises(CanonicalRuntimeError, match="absolute"):
        canonical_settings(Path("stage14b_canonical_v12.db"))


def test_preflight_is_read_only_and_reports_every_section(canonical_db):
    before = canonical_db.read_bytes()
    report = run_preflight(canonical_db)
    assert report.ok, report.problems
    assert canonical_db.read_bytes() == before
    assert report.sha256_before == report.sha256_after
    text = canonical_runtime.render_preflight(report)
    for section in (
        "DATABASE TARGET",
        "OUTBOUND STATE",
        "ALEMBIC STATE",
        "MATCHER VERSION",
        "MIGRATION HEAD",
    ):
        assert section in text
    assert "outbound_sending_enabled=false" in text
    assert "alembic_auto_upgrade=false" in text
    assert "RESULT: PASS" in text


def test_preflight_flags_wrong_revision_and_unexpected_digest(canonical_db):
    with sqlite3.connect(canonical_db) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'deadbeef'")
    report = run_preflight(canonical_db, expected_sha256="0" * 64)
    assert "DATABASE_NOT_AT_MIGRATION_HEAD" in report.problems
    assert "DATABASE_SHA256_UNEXPECTED" in report.problems
    assert not report.ok


def test_runtime_cli_preflight_exit_codes(canonical_db, capsys):
    # preflight keeps an optional, diagnostic-only digest
    assert runtime_cli.main(["preflight", "--database", str(canonical_db)]) == 0
    assert "RESULT: PASS" in capsys.readouterr().out
    assert runtime_cli.main(["preflight", "--database", str(canonical_db.with_name("x.db"))]) == 2
    assert "does not exist" in capsys.readouterr().err


def _sha(path: Path) -> str:
    return canonical_runtime.file_sha256(path)


def _other_db_at_head(canonical_db: Path, name: str, marker: str) -> Path:
    """Another database at the SAME migration head as canonical (like the audit
    or pilot DB): only its content, hence its digest, differs."""
    path = canonical_db.with_name(name)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", expected_alembic_heads())
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES (?)", (marker,))
    return path


def _serve(database: Path, *extra: str):
    """Run `serve` with a recording callback; the process env is restored."""
    started = []
    saved = dict(os.environ)
    try:
        code = runtime_cli.main(
            ["serve", "--database", str(database), *extra],
            start_server=lambda port: started.append(
                (
                    port,
                    os.environ["DATABASE_URL"],
                    {flag: os.environ[flag.upper()] for flag in SAFETY_FLAGS},
                )
            ),
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, started


def test_serve_without_expected_sha_is_refused_and_never_starts(canonical_db, capsys):
    code, started = _serve(canonical_db, "--port", "8123")  # A
    assert code == 2
    assert started == []
    assert "serve requires --expect-sha256" in capsys.readouterr().err


def test_serve_canonical_with_the_approved_sha_starts_exactly_once(canonical_db):
    code, started = _serve(canonical_db, "--expect-sha256", _sha(canonical_db), "--port", "8123")
    assert code == 0  # B
    assert len(started) == 1
    port, url, flags = started[0]
    assert (port, url) == (8123, f"sqlite:///{canonical_db.as_posix()}")
    assert set(flags.values()) == {"false"}  # G: every safety flag forced off
    assert flags["outbound_sending_enabled"] == "false"
    assert flags["alembic_auto_upgrade"] == "false"


@pytest.mark.parametrize(
    "name", ["stage14b_working.db", "stage11_pilot.db"]
)  # C, D: wrong DB at the same migration head
def test_serve_refuses_another_database_given_the_canonical_sha(canonical_db, capsys, name):
    approved = _sha(canonical_db)
    other = _other_db_at_head(canonical_db, name, marker=name)
    assert _sha(other) != approved
    assert run_preflight(other).ok  # path + head + flags alone would pass
    code, started = _serve(other, "--expect-sha256", approved)
    assert code == 1
    assert started == []
    assert "DATABASE_SHA256_UNEXPECTED" in capsys.readouterr().out


def test_serve_refuses_the_canonical_database_with_a_wrong_sha(canonical_db):
    for wrong in ("0" * 64, "F" * 64):  # E
        code, started = _serve(canonical_db, "--expect-sha256", wrong)
        assert code == 1 and started == []
    code, started = _serve(canonical_db, "--expect-sha256", "not-a-digest")
    assert code == 2 and started == []


def test_serve_refuses_a_migration_head_mismatch_even_with_the_matching_sha(canonical_db):
    with sqlite3.connect(canonical_db) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'deadbeef'")
    code, started = _serve(canonical_db, "--expect-sha256", _sha(canonical_db))  # F
    assert code == 1
    assert started == []


# --- 3. application contact data ------------------------------------------------


def test_synthetic_identity_is_valid_and_masked():
    identity = _identity()
    assert "erika" not in repr(identity).casefold()
    assert "1234567" not in str(identity)
    assert contact_problems_for_names(identity, "Erika", "Mustermann") == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"phone": "call me"},
        {"phone": "12"},
        {"full_name": "Erika\nMustermann"},
        {"city": "   "},
        {"confirmed_by": "SCRAPER"},
    ],
)
def test_identity_rejects_invalid_values_without_echoing_them(overrides):
    with pytest.raises(ValidationError) as excinfo:
        _identity(**overrides)
    assert "not-an-email" not in str(excinfo.value)


def test_contact_problems():
    assert contact_problems_for_names(None, "Erika", "Mustermann") == (CONTACT_DATA_MISSING,)
    assert contact_problems_for_names(_identity(phone=None), "Erika", "Mustermann") == (
        CONTACT_PHONE_MISSING,
    )
    assert contact_problems_for_names(_identity(), "Max", "Mustermann") == (CONTACT_NAME_MISMATCH,)
    assert contact_problems_for_names(_identity(), None, "Mustermann") == (
        CONTACT_PROFILE_NAME_UNTRUSTED,
    )


def test_identity_file_loading(tmp_path):
    assert load_application_identity(None) is None
    good = tmp_path / "identity.json"
    good.write_text(_identity().model_dump_json(), encoding="utf-8")
    assert load_application_identity(good) == _identity()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**json.loads(good.read_text()), "email": "x@@y"}), "utf-8")
    with pytest.raises(ApplicationIdentityInvalidError, match="email") as excinfo:
        load_application_identity(bad)
    assert "x@@y" not in str(excinfo.value)
    with pytest.raises(ApplicationIdentityInvalidError):
        load_application_identity(tmp_path / "absent.json")


# --- 2. CV document / DOCX / filenames / attachment plan ----------------------


def test_cv_document_without_contact_data_reports_it_and_shows_no_contact_lines():
    document = build_cv_document(_cv(), None)
    text = _all_text(document.blocks)
    assert document.warnings == (CONTACT_DATA_MISSING,)
    assert "E-Mail" not in text and "Telefon" not in text
    assert "Wohnort: Hannover" in text


def test_cv_document_is_german_ordered_and_only_uses_draft_facts():
    document = build_cv_document(_cv(), _identity())
    assert document.warnings == ()
    headings = [b.text for b in document.blocks if b.kind == "HEADING"]
    assert headings == [
        "Profil",
        "Kenntnisse",
        "Projekte",
        "Berufserfahrung",
        "Ausbildung",
        "Sprachen",
    ]
    text = _all_text(document.blocks)
    assert "E-Mail: erika.mustermann@example.org" in text
    assert "Telefon: +49 511 1234567" in text
    assert "Adresse: 30159 Hannover" in text
    assert "IT-Support – Beispiel GmbH" in text
    assert "aktuell" in text  # current job without a start date: no invented date
    assert "03/2025 – 06/2025" in text
    assert "nicht abgeschlossen" in text  # unfinished education never shown as a degree
    # UNKNOWN stays unknown: the language is listed, no level is invented and
    # no placeholder/enum text reaches the employer (V1 quality patch).
    assert "\nEnglisch\n" in f"\n{text}\n"
    assert "Englisch –" not in text and "Niveau nicht angegeben" not in text
    assert "Deutsch – B1" in text
    assert "Zertifikate" not in headings  # empty sections are left out


def test_docx_is_valid_deterministic_and_escaped():
    blocks = build_cv_document(_cv(), _identity()).blocks
    first, second = export.render_docx(blocks), export.render_docx(blocks)
    assert first == second
    with zipfile.ZipFile(__import__("io").BytesIO(first)) as archive:
        assert archive.namelist()[0] == "[Content_Types].xml"
        xml = archive.read("word/document.xml").decode("utf-8")
        ElementTree.fromstring(xml)
        ElementTree.fromstring(archive.read("word/styles.xml"))
    assert "Support &lt;intern&gt; &amp; Wartung" in xml


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Müller & Söhne GmbH", "Lebenslauf_Erika_Mustermann_Mueller-Soehne-GmbH_REF-1.docx"),
        ("..\\..\\evil/../x", "Lebenslauf_Erika_Mustermann_evil-x_REF-1.docx"),
        (
            "Finanz Informatik GmbH & Co. KG",
            "Lebenslauf_Erika_Mustermann_Finanz-Informatik-GmbH-Co-KG_REF-1.docx",
        ),
    ],
)
def test_filenames_are_deterministic_and_sanitized(company, expected):
    name = export.document_filename(
        "Lebenslauf", first_name="Erika", last_name="Mustermann", company=company, reference="REF 1"
    )
    assert name == expected
    assert "/" not in name and "\\" not in name and ".." not in name


def test_write_document_never_overwrites_outside_a_temp_dry_run_dir(tmp_path):
    first = export.write_document(b"one", tmp_path, "Lebenslauf_A.docx")
    assert first.path.read_bytes() == b"one"
    with pytest.raises(export.DocumentExistsError):
        export.write_document(b"two", tmp_path, "Lebenslauf_A.docx")
    assert first.path.read_bytes() == b"one"
    # an explicit dry-run directory under the system temp dir may be rewritten
    again = export.write_document(b"two", tmp_path, "Lebenslauf_A.docx", dry_run_directory=True)
    assert again.path.read_bytes() == b"two"
    with pytest.raises(export.UnsafeExportTargetError):
        export.write_document(b"x", _REPO / "docs", "Lebenslauf_A.docx", dry_run_directory=True)
    with pytest.raises(export.UnsafeExportTargetError):
        export.write_document(b"x", tmp_path, "../escape.docx")


def _reviewed_letter() -> ReviewedBewerbungContent:
    return ReviewedBewerbungContent(
        subject=ReviewTextField(value="Bewerbung als Entwickler"),
        salutation=ReviewTextField(value="Sehr geehrte Damen und Herren,"),
        opening=ReviewTextField(value="mit Interesse habe ich Ihre Anzeige gelesen."),
        body_paragraphs=[ReviewedBewerbungParagraph(text="Ich bringe Kenntnisse in Python mit.")],
        closing=ReviewTextField(value="Mit freundlichen Grüßen"),
        signature_name=ReviewTextField(value="Erika Mustermann"),
    )


def test_package_export_and_attachment_plan_without_contact_data(tmp_path):
    attachments, warnings = export.export_package_documents(
        cv=_cv(),
        reviewed_bewerbung=_reviewed_letter(),
        identity=None,
        company="Beispiel AG",
        reference="REF-1",
        directory=tmp_path,
    )
    assert [a.role for a in attachments] == ["CV", "COVER_LETTER"]
    assert attachments[0].document.filename == "Lebenslauf_Erika_Mustermann_Beispiel-AG_REF-1.docx"
    assert attachments[1].document.filename == "Anschreiben_Erika_Mustermann_Beispiel-AG_REF-1.docx"
    assert all(a.document.path.is_file() for a in attachments)
    plan = export.build_attachment_plan(
        job_id=1,
        vacancy_reference="REF-1",
        channel="APPLICATION_PORTAL",
        attachments=attachments,
        document_warnings=warnings,
        review_status="PENDING_REVIEW",
        readiness_blockers=("APPLICATION_CHANNEL_VERIFIED",),
    )
    assert plan.delivery == "NOT_SENT"
    assert plan.blockers == (
        CONTACT_DATA_MISSING,
        export.APPROVAL_PENDING,
        "APPLICATION_CHANNEL_VERIFIED",
    )
    assert not plan.ready_for_human_handoff
    rendered = export.render_attachment_plan(plan)
    assert "delivery: NOT_SENT" in rendered and "ready for human handoff: NO" in rendered


def test_attachment_plan_with_contact_data_still_waits_for_approval(tmp_path):
    attachments, warnings = export.export_package_documents(
        cv=_cv(),
        reviewed_bewerbung=_reviewed_letter(),
        identity=_identity(),
        company="Beispiel AG",
        reference="REF-1",
        directory=tmp_path,
    )
    assert warnings == ()
    pending = export.build_attachment_plan(
        job_id=1,
        vacancy_reference="REF-1",
        channel="APPLICATION_PORTAL",
        attachments=attachments,
        document_warnings=warnings,
        review_status="PENDING_REVIEW",
    )
    assert pending.blockers == (export.APPROVAL_PENDING,)
    approved = export.build_attachment_plan(
        job_id=1,
        vacancy_reference="REF-1",
        channel="APPLICATION_PORTAL",
        attachments=attachments,
        document_warnings=warnings,
        review_status="APPROVED",
    )
    assert approved.ready_for_human_handoff and approved.delivery == "NOT_SENT"


def test_export_modules_have_no_network_or_mail_capability():
    forbidden = {"httpx", "requests", "smtplib", "socket", "imaplib", "urllib"}
    for module in ("app/services/document_export.py", "app/agents/cv_document.py"):
        tree = ast.parse((_REPO / module).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert not imported & forbidden, module


# --- 4. stable vacancy identity --------------------------------------------------


def _e362_job(job_id: int = 1) -> JobRecord:
    return JobRecord(
        id=job_id,
        fingerprint=_E362_FINGERPRINT,
        source="bundesagentur",
        title="E362/B - AI Engineer / KI-Entwickler (m/w/d)",
        company="Finanz Informatik GmbH & Co. KG",
        location="Frankfurt am Main",
        url=_E362_URL,
    )


def _committed_destination() -> ApplicationDestination:
    raw = json.loads((_REPO / "docs/research/stage14a_e362b_destination.json").read_text("utf-8"))
    return ApplicationDestination.model_validate(raw)


def test_committed_research_is_bound_by_stable_identity_not_pilot_job_id():
    destination = _committed_destination()
    assert destination.vacancy is not None and destination.job_id is None
    canonical, pilot = _e362_job(1), _e362_job(27)
    assert canonical.id != pilot.id
    assert destination_matches_job(destination, canonical)
    assert destination_matches_job(destination, pilot)


def test_stable_identity_rejects_another_vacancy():
    destination = _committed_destination()
    other = _e362_job(1)
    other.url = "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-OTHERREF-S"
    assert not destination_matches_job(destination, other)
    other = _e362_job(1)
    other.fingerprint = "0" * 64
    assert not destination_matches_job(destination, other)
    other = _e362_job(1)
    other.source = "xing"
    assert not destination_matches_job(destination, other)


def test_vacancy_identity_requires_a_stable_key_and_job_id_is_legacy_only():
    with pytest.raises(ValidationError):
        VacancyIdentity(source="bundesagentur")
    with pytest.raises(ValidationError):
        VacancyIdentity(source="bundesagentur", fingerprint="NOT-HEX")
    with pytest.raises(ValidationError):
        ApplicationDestination(kind="UNKNOWN", reason="nothing")
    legacy = ApplicationDestination(job_id=27, kind="UNKNOWN", reason="legacy")
    assert destination_matches_job(legacy, _e362_job(27))
    assert not destination_matches_job(legacy, _e362_job(1))


# --- 5. CLI: clean BLOCKED results, no traceback ---------------------------------


@pytest.fixture()
def e362_db(tmp_path, monkeypatch):
    """Canonical-shaped DB: E362/B as job 1 (pilot was 27), trusted profile."""
    monkeypatch.chdir(tmp_path)  # never read the operator's .env
    monkeypatch.setenv("OUTBOUND_SENDING_ENABLED", "false")
    path = tmp_path / "canonical.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        record = get_or_create_candidate_profile(db)
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=record.profile_version,
                first_name="Erika",
                last_name="Mustermann",
            ),
        )
        job = _e362_job(1)
        job.description = "Python"
        job.skills_json = job.must_have_skills_json = '["python"]'
        job.nice_to_have_skills_json = "[]"
        job.score, job.recommendation, job.status = 83, "APPLY", "NEW"
        job.first_seen_at = job.last_seen_at = _NOW
        db.add(job)
        db.commit()
    engine.dispose()
    return path


def _cli(db_path: Path, *extra: str) -> list[str]:
    url = f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true"
    return ["--database-url", url, "--job-id", "1", *extra]


def test_cli_resolves_committed_research_on_canonical_job_1(e362_db, capsys):
    code = cli.main(
        _cli(
            e362_db,
            "--destination-json",
            str(_REPO / "docs/research/stage14a_e362b_destination.json"),
            "--location-json",
            str(_REPO / "docs/research/stage14a_e362b_locations.json"),
        )
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err + captured.out
    assert "READY_FOR_SEND_PACKAGE = NO (job_id=1)" in captured.out
    assert "[PASS] RECIPIENT_TYPE_KNOWN: kind=APPLICATION_PORTAL" in captured.out
    assert "[FAIL] APPLICATION_CHANNEL_VERIFIED" in captured.out
    assert "[FAIL] APPLICATION_CONTACT_DATA_COMPLETE: CONTACT_DATA_MISSING" in captured.out
    assert "JOB_LOCATION_NOT_IN_AUTHORITATIVE_LOCATIONS" in captured.out


def test_cli_reports_a_foreign_destination_as_blocked_without_traceback(e362_db, tmp_path, capsys):
    foreign = tmp_path / "foreign.json"
    foreign.write_text(
        json.dumps(
            {
                "vacancy": {"source": "bundesagentur", "source_reference": "10000-OTHERREF-S"},
                "kind": "UNKNOWN",
                "reason": "other vacancy",
            }
        ),
        encoding="utf-8",
    )
    code = cli.main(_cli(e362_db, "--destination-json", str(foreign)))
    captured = capsys.readouterr()
    assert code == 1
    assert f"BLOCKED {DESTINATION_VACANCY_MISMATCH}" in captured.out
    assert "Traceback" not in captured.err + captured.out


def test_cli_reports_foreign_location_evidence_as_blocked(e362_db, tmp_path, capsys):
    locations = tmp_path / "locations.json"
    locations.write_text(
        json.dumps(
            [
                {
                    "vacancy": {"source": "bundesagentur", "fingerprint": "1" * 64},
                    "source_type": "JOB_BOARD_LISTING",
                    "locations": ["Berlin"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert cli.main(_cli(e362_db, "--location-json", str(locations))) == 1
    assert f"BLOCKED {LOCATION_EVIDENCE_VACANCY_MISMATCH}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flag", "content"),
    [
        ("--destination-json", "{not json"),
        ("--destination-json", json.dumps({"kind": "EMAIL", "value": "x"})),
        ("--location-json", json.dumps({"not": "a list"})),
        ("--identity-json", json.dumps({"full_name": "Erika Mustermann"})),
    ],
)
def test_cli_refuses_invalid_input_files_cleanly(e362_db, tmp_path, capsys, flag, content):
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")
    assert cli.main(_cli(e362_db, flag, str(path))) == 2
    captured = capsys.readouterr()
    assert "refused: INPUT_INVALID" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_cli_with_synthetic_identity_passes_the_contact_check(e362_db, tmp_path, capsys):
    identity = tmp_path / "identity.json"
    identity.write_text(_identity().model_dump_json(), encoding="utf-8")
    assert cli.main(_cli(e362_db, "--identity-json", str(identity))) == 1  # still blocked
    out = capsys.readouterr().out
    assert "[PASS] APPLICATION_CONTACT_DATA_COMPLETE" in out
    assert "[PASS] OUTBOUND_STILL_DISABLED" in out
    assert "erika.mustermann@example.org" not in out


def test_loader_raises_typed_mismatch_for_location_evidence(e362_db):
    engine = create_engine(f"sqlite:///{e362_db}")
    try:
        with sessionmaker(bind=engine)() as db:
            with pytest.raises(VacancyEvidenceMismatchError) as excinfo:
                load_send_readiness_inputs(
                    db,
                    db.get(JobRecord, 1),
                    settings=__import__("app.core.config", fromlist=["Settings"]).Settings(
                        _env_file=None
                    ),
                    destination=None,
                    location_evidence=[
                        LocationEvidence(
                            source_type="JOB_BOARD_LISTING",
                            vacancy=VacancyIdentity(
                                source="bundesagentur", source_reference="10000-OTHERREF-S"
                            ),
                        )
                    ],
                )
            assert excinfo.value.code == LOCATION_EVIDENCE_VACANCY_MISMATCH
    finally:
        engine.dispose()


# --- 6. Bewerbung language relevance ----------------------------------------------

_LANGUAGES = [
    CVLanguageItem(source_id=1, language="Bulgarisch", level="B2", certificate=None),
    CVLanguageItem(source_id=2, language="Deutsch", level="B1", certificate=None),
    CVLanguageItem(source_id=3, language="Englisch", level="UNKNOWN", certificate=None),
    CVLanguageItem(source_id=4, language="Russisch", level="NATIVE", certificate=None),
]


def _language_claims(description: str, languages=_LANGUAGES, match=None) -> list[str]:
    evidence, _ = build_evidence(
        _cv(languages=languages),
        match or _match(),
        "Entwickler (m/w/d)",
        "Beispiel AG",
        description,
    )
    return [c.claim for c in evidence.allowed_claims if c.source_entity == "candidate_language"]


def test_german_posting_cites_german_and_never_unrelated_bulgarian():
    assert posting_language(_GERMAN_AD) == "de"
    assert _language_claims(_GERMAN_AD) == ["Deutsch (B1)"]


def test_a_named_language_is_relevant_and_ranked_before_the_posting_language():
    claims = _language_claims(_GERMAN_AD + " Bulgarischkenntnisse sind von Vorteil.")
    assert claims == ["Bulgarisch (B2)", "Deutsch (B1)"]


def test_english_posting_without_a_known_english_level_claims_no_language():
    assert posting_language(_ENGLISH_AD) == "en"
    assert _language_claims(_ENGLISH_AD) == []  # English UNKNOWN stays unclaimed


def test_short_text_and_country_name_do_not_make_a_language_relevant():
    assert posting_language("Python in Deutschland.") is None
    assert _language_claims("Python-Entwicklung in Deutschland.") == []


def test_a_language_requirement_ranks_first():
    languages = [
        CVLanguageItem(source_id=2, language="Deutsch", level="B1", certificate=None),
        CVLanguageItem(
            source_id=4,
            language="Russisch",
            level="NATIVE",
            certificate=None,
            matched_requirement="Russisch",
            match_status="MATCH",
        ),
    ]
    assert relevant_languages(languages, job_title="", job_description=_GERMAN_AD)[0].language == (
        "Russisch"
    )


def test_unknown_level_is_never_claimed_even_when_required():
    english = CVLanguageItem(
        source_id=3,
        language="Englisch",
        level="UNKNOWN",
        certificate=None,
        matched_requirement="English",
        match_status="UNKNOWN",
    )
    assert _language_claims("Fließend Englisch erforderlich.", languages=[english]) == []


def test_language_key_table():
    assert language_key("Deutsch") == language_key("German") == "de"
    assert language_key("Klingonisch") is None


def test_e362b_like_profile_letter_evidence(tmp_path):
    """The E362/B dry-run finding: Bulgarian B2 must not be cited for a German
    posting that names no language; German B1 is relevant via the ad's language."""
    claims = _language_claims(_GERMAN_AD, match=_match([_requirement_python()]))
    assert "Bulgarisch (B2)" not in claims
    assert claims == ["Deutsch (B1)"]


def _requirement_python() -> RequirementMatch:
    return RequirementMatch(
        requirement="python",
        normalized_requirement="python",
        requirement_type="SKILL",
        importance="REQUIRED",
        match_status="MATCH",
        reason="synthetic",
    )


def test_profile_language_model_still_accepts_unknown_level():
    assert CandidateLanguage(language="Englisch").level == "UNKNOWN"
