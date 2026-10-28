"""Stage 19: end-to-end operational rehearsal on an isolated database.

One vacancy lifecycle through the project's real entry points:
Bundesagentur collector (real parser and scoring, mocked HTTP) ->
normalisation and deduplication -> matcher via the API -> Stage 17 fit
criteria + Stage 19 fit tier (ranking) -> CV/Bewerbung drafts and review
package via the API -> pre-submit check CLI -> application tracker CLI ->
employer responses (classification + correlation) -> operator transition ->
follow-up dates.

Fictional candidate and employers only. No network, no SMTP/IMAP/Telegram;
DATABASE_ROLE=test refuses the production runtime database.
"""

import asyncio
import base64
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import BundesagenturCollector
from app.core.config import Settings
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.candidate_search_repository import apply_config_plan, plan_config_change
from app.db.job_commute_repository import record_commute_check
from app.db.models import ApplicationEventRecord, CandidateProfileRecord, JobRecord
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R
from app.models.candidate_profile import CandidateProfilePatchRequest, CandidateSkill
from app.models.candidate_search import CandidateConfigSpec, CandidateSearchSettings, GeoPoint
from app.services import collector_runner
from app.services import job_fit_assessment as fit
from app.services.application_correlation import InboundMessage
from app.services.application_response_processor import (
    NEEDS_OPERATOR_REVIEW,
    ApplicationResponseProcessor,
)
from app.services.application_tracker import ApplicationTracker
from app.services.pre_submit_validation import write_manifest
from scripts import presubmit_check
from scripts import stage15_application_tracker as tracker_cli
from tests.test_night_audit_presubmit import _pdf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGIN = GeoPoint(lat=50.5, lon=9.0)  # fictional "Musterstadt Bahnhof"
EMAIL = "max.muster@example.invalid"
PHONE = "+49 151 00000000"
API_KEY = "stage19-test-key"

SEARCH = CandidateSearchSettings(
    home_city="Musterstadt",
    home_postal_code="63000",
    home_region="Hessen",
    commute_origin_label="Musterstadt Bahnhof",
    commute_origin=ORIGIN,
    max_commute_km=100,
    commute_measure="CAR_ROUTE",
    relocation_regions=["Hessen"],
    remote_work_scope="GERMANY",
    remote_work_from_region="Hessen",
    minimum_salary=45000,
    maximum_salary=50000,
    salary_negotiable=True,
    earliest_start_date=date(2027, 1, 1),
)
SPEC = {
    "schema": "jobtriage.candidate-config/1",
    "source": "USER_CONFIRMED",
    "confirmed_on": "2026-10-28",
    "search": json.loads(SEARCH.model_dump_json()),
    "languages": [{"language": "Deutsch", "level": "B1"}],
}
SKILLS = ["Python", "FastAPI", "Django", "Flask", "SQL", "SQLAlchemy", "PostgreSQL"]
SKILLS += ["Docker", "Git", "REST API", "pytest"]

# Real BA descriptions carry company and benefits text; without it the
# scorer rightly answers NEEDS_ENRICHMENT (too little evidence).
BOILERPLATE = (
    " Über uns: Wir sind ein mittelständisches Unternehmen mit rund 120 Mitarbeitenden "
    "und entwickeln Softwarelösungen für Kunden aus Industrie und Handel. Unser Team "
    "arbeitet agil in kleinen Gruppen mit kurzen Entscheidungswegen. Wir bieten: "
    "unbefristeten Arbeitsvertrag, 30 Tage Urlaub, betriebliche Altersvorsorge, "
    "Weiterbildungsbudget, moderne Arbeitsplätze, Jobticket und eine strukturierte "
    "Einarbeitung mit festem Ansprechpartner. Ihre Bewerbung senden Sie bitte über "
    "unser Karriereportal mit Lebenslauf und Anschreiben."
) * 2

JUNIOR = "10001-0000000001-S"
SENIOR = "10001-0000000002-S"
C2_JOB = "10001-0000000003-S"
FAR = "10001-0000000004-S"
QA = "10001-0000000005-S"
MIRROR = "10001-0000000006-S"


def _posting(ref, title, firma, ort, plz):
    return {
        "referenznummer": ref,
        "stellenangebotsTitel": title,
        "firma": firma,
        "stellenlokationen": [{"adresse": {"ort": ort, "plz": plz, "region": "HESSEN"}}],
        "datumErsteVeroeffentlichung": "2026-10-27",
    }


POSTINGS = {
    JUNIOR: (
        _posting(
            JUNIOR,
            "Junior Python Backend Developer (m/w/d)",
            "Beispiel Software GmbH",
            "Hanau",
            "63450",
        ),
        "Wir suchen eine:n Junior Python Backend Developer (m/w/d) für unser Team in "
        "Hanau. Ihre Aufgaben: REST-APIs mit Python und FastAPI entwickeln, Datenbanken "
        "mit PostgreSQL und SQLAlchemy, Container mit Docker, Versionierung mit Git, "
        "Tests mit pytest. Ihr Profil: Berufseinsteiger willkommen, Deutschkenntnisse B1, "
        "B2 wünschenswert. Gehalt zwischen 42.000 und 48.000 € brutto im Jahr. Hybrid: "
        "2 Tage im Büro. Arbeitsort: Industriestraße 5, 63450 Hanau. Start ab 01.01.2027."
        + BOILERPLATE,
    ),
    SENIOR: (
        _posting(
            SENIOR,
            "Senior AWS Cloud Architect (m/w/d)",
            "Wolkenbau AG",
            "Frankfurt am Main",
            "60311",
        ),
        "Senior AWS Cloud Architect mit mindestens 8 Jahren Berufserfahrung in AWS, "
        "Terraform, Kubernetes und Python. Führungserfahrung erforderlich. Deutsch C1 "
        "zwingend erforderlich. Gehalt 95.000 € p.a." + BOILERPLATE,
    ),
    C2_JOB: (
        _posting(
            C2_JOB,
            "Python Entwickler (m/w/d) Behördensoftware",
            "Amtsdaten GmbH",
            "Offenbach am Main",
            "63065",
        ),
        "Python Entwickler mit Django, PostgreSQL, Docker und Git. Deutsch auf C2-Niveau "
        "zwingend erforderlich (Kundenkommunikation mit Behörden). Berufseinsteiger "
        "möglich." + BOILERPLATE,
    ),
    FAR: (
        _posting(FAR, "Junior Python Developer (m/w/d)", "Nordhafen Tech GmbH", "Hamburg", "20095"),
        "Junior Python Developer mit FastAPI, PostgreSQL, Docker und Git. Deutsch B1. "
        "Kein Homeoffice, Arbeitsort: Hafenstraße 1, 20095 Hamburg." + BOILERPLATE,
    ),
    QA: (
        _posting(
            QA,
            "Junior QA Engineer Python Testautomatisierung (m/w/d)",
            "Prüfwerk GmbH",
            "Darmstadt",
            "64283",
        ),
        "Testautomatisierung mit Python und pytest, CI mit Git und Docker, REST API "
        "Tests, SQL. Berufseinsteiger willkommen. Deutsch B1. Hybrid." + BOILERPLATE,
    ),
}
# An agency mirror of the junior vacancy: re-worded title, same city.
MIRROR_POSTING = (
    _posting(
        MIRROR,
        "Python Backend Entwickler Junior (m/w/d)",
        "Hays Professional Solutions GmbH",
        "Hanau",
        "63450",
    ),
    "Für unseren Kunden in Hanau suchen wir eine:n Junior Python Backend Entwickler mit "
    "FastAPI, PostgreSQL, Docker und Git. Deutsch B1." + BOILERPLATE,
)


def _alembic(path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "stage19_e2e.db"


@pytest.fixture()
def db(db_path):
    upgrade(_alembic(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine)()
    profile = get_or_create_candidate_profile(session)
    apply_config_plan(
        session,
        plan_config_change(profile, CandidateConfigSpec.model_validate(SPEC)),
        actor="test",
        reason="stage19 fixture",
    )
    profile = get_or_create_candidate_profile(session)
    apply_candidate_profile_patch(
        session,
        CandidateProfilePatchRequest(
            expected_profile_version=profile.profile_version,
            skills=[CandidateSkill(name=name) for name in SKILLS],
        ),
    )
    record = session.scalars(select(CandidateProfileRecord)).one()
    record.contact_email, record.contact_phone = EMAIL, PHONE
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def api(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app
    from app.security import rate_limit as rate_limit_module

    def override_get_db():
        yield db

    settings = Settings(_env_file=None, api_key=API_KEY, telegram_bot_token="")
    for target in ("app.security.auth", "app.security.rate_limit", "app.api.routes"):
        monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_get_db
    for name in dir(rate_limit_module):
        bucket = getattr(rate_limit_module, name)
        if name.startswith("_") and not name.startswith("__") and isinstance(bucket, dict):
            bucket.clear()
    with TestClient(app) as client:
        client.headers.update({"X-API-Key": API_KEY})
        yield client
    app.dependency_overrides.clear()


def _transport(postings):
    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if "/jobdetails/" in path:
            ref = base64.b64decode(path.rsplit("/", 1)[1]).decode("utf-8")
            if ref not in postings:
                return httpx.Response(404, json={"messages": [{"code": "NICHT_GEFUNDEN"}]})
            return httpx.Response(200, json={"stellenangebotsBeschreibung": postings[ref][1]})
        page = [posting for posting, _ in postings.values()]
        return httpx.Response(
            200, json={"ergebnisliste": page, "maxErgebnisse": len(page), "page": 1, "size": 100}
        )

    return httpx.MockTransport(handler)


class _NoNotifier:
    """Fails the test if anything tries to notify (Codex review: the
    environment may hold a live bot token)."""

    def __init__(self, *args, **kwargs):
        pass

    async def send_job(self, job, score):
        raise AssertionError("the rehearsal must never notify")


def _collect(db, monkeypatch, postings=POSTINGS, settings=None):
    transport = _transport(postings)
    if settings is None:
        monkeypatch.setattr(collector_runner, "TelegramNotifier", _NoNotifier)

    def factory(**kwargs):
        return BundesagenturCollector(http_client=httpx.AsyncClient(transport=transport), **kwargs)

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", factory)
    settings = settings or Settings(
        _env_file=None,
        bundesagentur_api_key="test-key",
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_vacancy_feed_enabled=False,
        company_research_auto_enabled=False,
    )
    return asyncio.run(collector_runner.run_bundesagentur(db, settings))


def _jobs(db) -> dict[str, JobRecord]:
    return {job.url.rsplit("/", 1)[1]: job for job in db.scalars(select(JobRecord))}


def _route(db, job, km, *, destination, method="OSRM"):
    record_commute_check(
        db,
        job_id=job.id,
        origin_label=SEARCH.commute_origin_label,
        destination_label=destination,
        destination_region="Hessen",
        origin_lat=ORIGIN.lat,
        origin_lon=ORIGIN.lon,
        car_route_km=km,
        car_route_minutes=km,
        method=method,
        source_note="stage19 rehearsal",
    )


def _ranked(db, api):
    jobs = _jobs(db)
    for job in jobs.values():
        assert api.post(f"/api/v1/jobs/{job.id}/match", json={}).status_code == 200
    _route(db, jobs[JUNIOR], 28.0, destination="Industriestraße 5, 63450 Hanau")
    _route(db, jobs[SENIOR], 25.0, destination="60311 Frankfurt am Main")
    _route(db, jobs[C2_JOB], 12.0, destination="63065 Offenbach am Main")
    _route(db, jobs[FAR], 495.0, destination="Hafenstraße 1, 20095 Hamburg")
    return jobs, {ref: fit.assess_job(db, job.id) for ref, job in jobs.items()}


# --- B1-B3: collection, normalisation, deduplication -------------------------------


def test_collection_is_normalised_and_idempotent(db, monkeypatch):
    first = _collect(db, monkeypatch)
    assert (first["fetched"], first["created"], first["failed"]) == (5, 5, 0)
    again = _collect(db, monkeypatch)
    assert (again["created"], again["updated"]) == (0, 5)
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 5
    jobs = _jobs(db)
    qa = jobs[QA]
    assert qa.company == "Prüfwerk GmbH"  # umlauts kept
    assert qa.location.startswith("Darmstadt")
    assert jobs[JUNIOR].url == f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{JUNIOR}"
    assert "Industriestraße 5, 63450 Hanau" in jobs[JUNIOR].description
    assert all(job.recommendation != "NEEDS_ENRICHMENT" for job in jobs.values())


def test_agency_mirror_is_a_new_row_but_flagged_after_applying(db, monkeypatch):
    from app.services.application_duplicates import VacancyView, classify_duplicate
    from app.services.application_guard import vacancy_findings

    _collect(db, monkeypatch, {**POSTINGS, MIRROR: MIRROR_POSTING})
    jobs = _jobs(db)
    assert len(jobs) == 6  # a mirror is its own posting, never merged away
    assert _submit_via_cli(db, jobs[JUNIOR]) is not None

    def grade(job):
        view = VacancyView(
            job.id, job.source, job.company, job.title, job.url, job.fingerprint, job.location
        )
        return classify_duplicate(view, vacancy_findings(db, job))

    assert grade(jobs[MIRROR]) in {"PROBABLE_DUPLICATE", "POSSIBLE_DUPLICATE"}
    # A different role at a different employer is never blocked.
    assert grade(jobs[QA]) == "NOT_DUPLICATE"


# --- B4-B5: matching and ranking ---------------------------------------------------


def test_fit_tiers_rank_good_partial_operator_and_unsuitable(db, api, monkeypatch):
    _collect(db, monkeypatch)
    _, assessed = _ranked(db, api)
    tiers = {ref: a.fit_tier for ref, a in assessed.items()}
    assert tiers[JUNIOR] == "GOOD_MATCH", assessed[JUNIOR].tier_reasons
    assert tiers[SENIOR] == "NOT_SUITABLE"
    # The explicit "Deutsch C1 zwingend" is the barrier; the 8 years count
    # as one only when the current matcher lists the experience as MISSING.
    assert any("German C1" in r for r in assessed[SENIOR].tier_reasons)
    assert assessed[SENIOR].criterion("experience").status == "GAP"
    assert tiers[C2_JOB] == "NOT_SUITABLE"
    assert any("German C2" in r for r in assessed[C2_JOB].tier_reasons)
    assert tiers[FAR] == "NOT_SUITABLE"
    assert assessed[FAR].decision == "BLOCKED"  # location FAIL: the Stage 17 hard rule
    # No measured route yet: the operator checks the distance first.
    assert tiers[QA] == "NEEDS_OPERATOR"
    assert any("DISTANCE_UNVERIFIED" in r for r in assessed[QA].tier_reasons)
    # The Stage 17 decision is unchanged: language/experience never block it.
    assert assessed[C2_JOB].decision == "CANDIDATE"
    junior = assessed[JUNIOR]
    assert junior.criterion("salary").status == "MATCH"
    assert "42,000-48,000" in junior.criterion("salary").reason
    assert junior.criterion("language").status == "OK"


def test_cli_assess_shows_the_tier_and_its_evidence(db, db_path, api, monkeypatch, capsys):
    from scripts import stage17_job_fit

    _collect(db, monkeypatch)
    jobs, _ = _ranked(db, api)
    db.commit()
    code = stage17_job_fit.run(["--database", str(db_path), "assess", str(jobs[C2_JOB].id)])
    out = capsys.readouterr().out
    assert code == 0 and "FIT TIER: NOT_SUITABLE" in out and "German C2" in out
    stage17_job_fit.run(["--database", str(db_path), "assess", str(jobs[JUNIOR].id), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["fit_tier"] == "GOOD_MATCH" and payload["tier_reasons"]


# --- C: documents and pre-submit ---------------------------------------------------


def _drafts(api, job_id):
    match = api.post(f"/api/v1/jobs/{job_id}/match", json={}).json()
    cv = api.post(f"/api/v1/jobs/{job_id}/cv-draft", json={"match_id": match["id"]}).json()
    letter = api.post(f"/api/v1/jobs/{job_id}/bewerbung-draft", json={"cv_draft_id": cv["id"]})
    assert letter.status_code == 200
    return cv, letter.json()


def _letter_text(letter) -> str:
    parts = [letter["salutation"], letter["opening"]]
    parts += [p["text"] for p in letter["body_paragraphs"]]
    parts += [letter["closing"]]
    return "\n".join(part for part in parts if part)


def _package(root, name, cv_text, letter_text):
    package = root / name
    (package / "_quelle_html").mkdir(parents=True)
    contact = f"{EMAIL} · {PHONE}"
    cv_lines = ["Max Muster", contact, *cv_text.splitlines()]
    letter_lines = [contact, *letter_text.splitlines()]
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(_pdf(2, cv_lines))
    (package / "Max_Muster_Anschreiben.pdf").write_bytes(_pdf(1, letter_lines))
    (package / "_quelle_html" / "_cv.html").write_text("\n".join(cv_lines), encoding="utf-8")
    (package / "_quelle_html" / "_letter.html").write_text(
        "\n".join(letter_lines), encoding="utf-8"
    )
    return package


def _presubmit(db_path, package, *extra):
    args = ["--database", str(db_path), "--package", str(package)]
    args += ["--company", "Beispiel Software GmbH"]
    args += ["--role", "Junior Python Backend Developer (m/w/d)", *extra]
    return presubmit_check.run(args)


def test_documents_are_truthful_and_presubmit_guards_the_upload(
    db, db_path, api, monkeypatch, tmp_path, capsys
):
    _collect(db, monkeypatch)
    jobs = _jobs(db)
    junior = jobs[JUNIOR].id
    cv, letter = _drafts(api, junior)
    text = _letter_text(letter)
    assert "Beispiel Software GmbH" in text and "Junior Python Backend Developer" in text
    assert letter["subject"] == "Bewerbung als Junior Python Backend Developer (m/w/d)"
    for overclaim in ("C1", "C2", "Muttersprache", "verhandlungssicher", "Berufserfahrung als"):
        assert overclaim not in text
    assert "B1" in text  # the confirmed level only
    # Every paragraph is bound to a confirmed profile fact.
    assert all(p["source_claim_ids"] for p in letter["body_paragraphs"])
    assert [lang["level"] for lang in cv["languages"]] == ["B1"]

    review = api.post(
        f"/api/v1/jobs/{junior}/review-package",
        json={"cv_draft_id": cv["id"], "bewerbung_draft_id": letter["id"]},
    ).json()
    assert review["status"] == "PENDING_REVIEW"
    approved = api.post(
        f"/api/v1/review-packages/{review['id']}/approve",
        json={"expected_review_version": review["review_version"]},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    cv_text = "Kenntnisse: " + ", ".join(s["text"] for s in cv["skills"]) + "\nDeutsch B1"
    package = _package(tmp_path / "packages", "beispiel_junior", cv_text, text)
    profile_version = db.scalars(select(CandidateProfileRecord)).one().profile_version
    write_manifest(package, profile_version)
    upload = tmp_path / "upload"
    upload.mkdir()
    cv_copy = shutil.copy(package / "Max_Muster_Lebenslauf.pdf", upload / "cv.pdf")
    letter_copy = shutil.copy(package / "Max_Muster_Anschreiben.pdf", upload / "letter.pdf")
    attach = ["--attach", f"cv={cv_copy}", "--attach", f"letter={letter_copy}"]
    capsys.readouterr()

    assert _presubmit(db_path, package, *attach) == 0, capsys.readouterr().out
    assert "READY" in capsys.readouterr().out
    # Without the upload copies it is never READY.
    assert _presubmit(db_path, package) == 3
    # The CV of another vacancy's package in the upload slot.
    other = _package(tmp_path / "packages", "anderer_job", "Kenntnisse: Java", text)
    wrong = ["--attach", f"cv={other / 'Max_Muster_Lebenslauf.pdf'}"]
    assert _presubmit(db_path, package, *wrong, "--attach", f"letter={letter_copy}") == 4
    # A required portal field the candidate has no confirmed value for.
    assert _presubmit(db_path, package, *attach, "--missing-field", "Zeugnisnote") == 4
    capsys.readouterr()
    # A missing package is refused.
    assert _presubmit(db_path, tmp_path / "nope") == 2


def test_presubmit_blocks_changed_pdf_and_stale_profile(db, db_path, tmp_path, capsys):
    letter = "Bewerbung als Junior Python Backend Developer (m/w/d) bei Beispiel Software GmbH"
    package = _package(tmp_path / "packages", "pkg", "Kenntnisse: Python\nDeutsch B1", letter)
    record = db.scalars(select(CandidateProfileRecord)).one()
    write_manifest(package, record.profile_version)
    upload = tmp_path / "u"
    upload.mkdir()
    cv_copy = shutil.copy(package / "Max_Muster_Lebenslauf.pdf", upload / "cv.pdf")
    attach = ["--attach", f"cv={cv_copy}", "--no-letter"]
    assert _presubmit(db_path, package, *attach) == 0
    # The CV is re-rendered (here: with an overclaim) after binding.
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(
        _pdf(2, ["Max Muster", f"{EMAIL} · {PHONE}", "Deutsch C1"])
    )
    shutil.copy(package / "Max_Muster_Lebenslauf.pdf", cv_copy)
    capsys.readouterr()
    assert _presubmit(db_path, package, *attach) == 4
    out = capsys.readouterr().out
    assert "CV_PDF_CHANGED" in out and "CV_GERMAN_OVERCLAIM" in out
    # The profile moved on since the package was built.
    write_manifest(package, record.profile_version - 1)
    assert _presubmit(db_path, package, *attach) == 4
    assert "PROFILE_VERSION_STALE" in capsys.readouterr().out
    # An empty PDF never passes.
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(b"")
    assert _presubmit(db_path, package, *attach) == 4


# --- D-E: tracker, responses, follow-up --------------------------------------------

# In the past, so every mail below precedes the operator's own transition.
SUBMITTED_AT = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)
APPLICANT = EMAIL


def _submit_via_cli(db, job, *, apply=True):
    path = db.get_bind().url.database
    reference = job.url.rsplit("/", 1)[1]
    args = ["--database", path, "record-submission", "--company", job.company]
    args += ["--role", job.title, "--source", "bundesagentur"]
    args += ["--reference", reference, "--job-id", str(job.id)]
    args += ["--channel", "EMPLOYER_PORTAL", "--submitted-at", SUBMITTED_AT.isoformat()]
    args += ["--employer-domain", "beispiel-software.example", "--actor", "operator"]
    args += ["--evidence", "presubmit READY; portal confirmation page"]
    if apply:
        args.append("--apply")
    db.commit()
    assert tracker_cli.run(args) == 0
    db.expire_all()
    apps = ApplicationTracker().list_applications(db)
    return next((a for a in apps if a.source_reference == reference), None)


def _mail(subject, body, *, sender, key, at, display=None):
    return InboundMessage(
        account_key=APPLICANT,
        message_key=f"mid:{key}",
        message_id_header=f"<{key}>",
        reply_to_ids=(),
        from_address=sender,
        from_display_name=display,
        to_addresses=(APPLICANT,),
        subject=subject,
        body=body,
        has_html=False,
        occurred_at=at,
    )


def _events(db, app_id):
    return db.scalar(
        select(func.count())
        .select_from(ApplicationEventRecord)
        .where(ApplicationEventRecord.application_id == app_id)
    )


def _status(db, app_id):
    db.expire_all()
    return ApplicationTracker().get_application(db, app_id).current_status


def test_application_lifecycle_from_submission_to_rejection(db, monkeypatch, capsys):
    _collect(db, monkeypatch)
    job = _jobs(db)[JUNIOR]
    # Dry run writes nothing.
    assert _submit_via_cli(db, job, apply=False) is None
    app = _submit_via_cli(db, job)
    assert app.current_status == S.SUBMITTED and app.job_id == job.id
    assert app.next_followup_at is not None
    assert app.next_followup_at - SUBMITTED_AT >= timedelta(days=7)
    # Recording the same Bewerbung again is idempotent.
    capsys.readouterr()
    assert _submit_via_cli(db, job).id == app.id
    assert "ALREADY_RECORDED" in capsys.readouterr().out
    events_after_submit = _events(db, app.id)
    # The same vacancy can no longer be "applied" again.
    path = db.get_bind().url.database
    assert tracker_cli.run(["--database", path, "check-job", str(job.id)]) == 3
    assert "ALREADY_APPLIED" in capsys.readouterr().out

    processor = ApplicationResponseProcessor()

    def process(*messages, apply=True):
        return processor.process(db, list(messages), apply=apply, applicant_addresses=(APPLICANT,))

    receipt = _mail(
        "Ihre Bewerbung als Junior Python Backend Developer (m/w/d)",
        "Vielen Dank für Ihre Bewerbung als Junior Python Backend Developer (m/w/d) bei "
        "Beispiel Software GmbH. Wir haben Ihre Bewerbung erhalten und melden uns.",
        sender="jobs@beispiel-software.example",
        key="receipt@beispiel",
        at=SUBMITTED_AT + timedelta(minutes=3),
    )
    [dry] = process(receipt, apply=False)
    assert dry.classification == R.RECEIPT_CONFIRMATION and not dry.event_recorded
    [result] = process(receipt)
    assert result.application_id == app.id and result.transition_applied
    assert _status(db, app.id) == S.RECEIVED
    events_after_receipt = _events(db, app.id)
    assert events_after_receipt > events_after_submit
    # Re-delivery of the same mail never adds an event.
    [again] = process(receipt)
    assert again.duplicate and _events(db, app.id) == events_after_receipt

    # "Es liegt keine Absage vor" is not a rejection.
    pending = _mail(
        "Ihre Bewerbung",
        "Wir können Ihnen derzeit keine positive Nachricht geben, es liegt keine Absage vor. "
        "Junior Python Backend Developer (m/w/d), Beispiel Software GmbH.",
        sender="jobs@beispiel-software.example",
        key="pending@beispiel",
        at=SUBMITTED_AT + timedelta(days=3),
    )
    [pending_result] = process(pending)
    assert pending_result.classification != R.REJECTION

    # An agency mail naming no role or employer is never attached to this one.
    agency = _mail(
        "Ihre Bewerbungen",
        "Leider müssen wir Ihnen mitteilen, dass wir uns anderweitig entschieden haben.",
        sender="team@personal-agentur.example",
        key="agency@agentur",
        at=SUBMITTED_AT + timedelta(days=4),
    )
    [agency_result] = process(agency)
    assert not agency_result.transition_applied and not agency_result.event_recorded
    assert _status(db, app.id) == S.RECEIVED

    # The employer's real rejection is recorded, never applied by mail.
    rejection = _mail(
        "Ihre Bewerbung bei Beispiel Software GmbH",
        "Vielen Dank für Ihr Interesse an der Position Junior Python Backend Developer (m/w/d). "
        "Leider müssen wir Ihnen mitteilen, dass wir uns anderweitig entschieden haben.",
        sender="jobs@beispiel-software.example",
        key="rejection@beispiel",
        at=SUBMITTED_AT + timedelta(days=9),
    )
    [rejected] = process(rejection)
    assert rejected.classification == R.REJECTION and rejected.application_id == app.id
    assert not rejected.transition_applied and rejected.proposed_status == "REJECTED"
    assert _status(db, app.id) == S.RECEIVED

    # The operator confirms it: dry run first, then the transition.
    db.commit()
    base = ["--database", path, "transition", str(app.id), "REJECTED"]
    base += ["--actor", "operator", "--reason", "Absage per Mail vom Arbeitgeber"]
    assert tracker_cli.run(base) == 0
    assert _status(db, app.id) == S.RECEIVED
    assert tracker_cli.run([*base, "--apply"]) == 0
    assert _status(db, app.id) == S.REJECTED
    final = ApplicationTracker().get_application(db, app.id)
    assert final.followup_required is False  # a terminal application needs none
    times = [e.occurred_at for e in ApplicationTracker().get_application_history(db, app.id)]
    assert times == sorted(times)


def test_operator_review_flag_for_sender_only_mail(db, monkeypatch):
    _collect(db, monkeypatch)
    app = _submit_via_cli(db, _jobs(db)[JUNIOR])
    processor = ApplicationResponseProcessor()
    receipt = _mail(
        "Ihre Bewerbung als Junior Python Backend Developer (m/w/d)",
        "Vielen Dank für Ihre Bewerbung als Junior Python Backend Developer (m/w/d) bei "
        "Beispiel Software GmbH.",
        sender="karriere@ats-portal.example",
        key="r2@ats",
        at=SUBMITTED_AT + timedelta(minutes=2),
    )
    processor.process(db, [receipt], apply=True, applicant_addresses=(APPLICANT,))
    vague = _mail(
        "Neuigkeiten zu Ihrer Bewerbung",
        "Wir laden Sie gerne zu einem Vorstellungsgespräch ein.",
        sender="karriere@ats-portal.example",
        key="v2@ats",
        at=SUBMITTED_AT + timedelta(days=5),
    )
    events = _events(db, app.id)
    [result] = processor.process(db, [vague], apply=True, applicant_addresses=(APPLICANT,))
    # Stage 19 fix: names no employer or role, but comes from the sender of
    # this application -- shown to the operator with the candidate, nothing
    # written (before, the invitation was dropped as NO_SIGNAL).
    assert result.classification == "INTERVIEW_INVITATION"
    assert result.operator_review == NEEDS_OPERATOR_REVIEW
    assert result.review_candidates == (app.id,)
    assert result.reason == "UNMATCHED_FROM_KNOWN_SENDER"
    assert not result.transition_applied and _events(db, app.id) == events
    # An unrelated sender's newsletter stays unflagged.
    other = _mail(
        "Einladung zum Webinar",
        "Wir laden Sie gerne zu einem Vorstellungsgespräch ein.",
        sender="news@other.example",
        key="n@other",
        at=SUBMITTED_AT + timedelta(days=5),
    )
    [unrelated] = processor.process(db, [other], apply=True, applicant_addresses=(APPLICANT,))
    assert unrelated.operator_review is None and unrelated.review_candidates == ()
    # An unrelated status change does not hide it (Codex review round 2) ...
    path = db.get_bind().url.database
    db.commit()
    other_move = ["--database", path, "transition", str(app.id), "UNDER_REVIEW"]
    assert tracker_cli.run([*other_move, "--actor", "operator", "--reason", "x", "--apply"]) == 0
    db.expire_all()
    [still] = processor.process(db, [vague], apply=True, applicant_addresses=(APPLICANT,))
    assert still.operator_review == NEEDS_OPERATOR_REVIEW
    # ... answering it does: the operator recorded the interview.
    move = ["--database", path, "transition", str(app.id), "INTERVIEW"]
    assert tracker_cli.run([*move, "--actor", "operator", "--reason", "Einladung", "--apply"]) == 0
    db.expire_all()
    [rescan] = processor.process(db, [vague], apply=True, applicant_addresses=(APPLICANT,))
    assert rescan.operator_review is None
    assert rescan.reason == "UNMATCHED_FROM_KNOWN_SENDER_HANDLED"


# --- resilience ----------------------------------------------------------------------


def test_stale_match_is_not_evidence(db, api, monkeypatch):
    """Codex Stage 19 review round 2: a posting changed after matching must
    not keep its old GOOD_MATCH."""
    _collect(db, monkeypatch)
    jobs, assessed = _ranked(db, api)
    assert assessed[JUNIOR].fit_tier == "GOOD_MATCH"
    job = jobs[JUNIOR]
    job.description += " Zwingend erforderlich: 5 Jahre Erfahrung mit Kubernetes und Go."
    db.commit()
    after = fit.assess_job(db, job.id)
    assert after.criterion("skills").status == "NOT_EVALUATED"
    assert "out of date" in after.criterion("skills").reason
    assert after.fit_tier == "NEEDS_OPERATOR" and after.decision == "NEEDS_VERIFICATION"


def test_failed_notification_never_loses_scored_jobs(db, monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def send_job(self, job, score):
            raise RuntimeError("telegram down")

    monkeypatch.setattr(collector_runner, "TelegramNotifier", Broken)
    settings = Settings(
        _env_file=None,
        bundesagentur_api_key="test-key",
        telegram_bot_token="123:abc",
        telegram_chat_id="1",
        min_job_score_to_notify=0,
    )
    result = _collect(db, monkeypatch, settings=settings)
    assert result["created"] == 5 and result["failed"] == 0
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 5


async def _no_sleep(_seconds):
    return None


def test_upstream_outage_reports_and_keeps_existing_jobs(db, monkeypatch):
    _collect(db, monkeypatch)

    def down(request):
        return httpx.Response(503, text="maintenance")

    def factory(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(down))
        return BundesagenturCollector(http_client=client, sleep=_no_sleep, **kwargs)

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", factory)
    settings = Settings(_env_file=None, bundesagentur_api_key="test-key")
    with pytest.raises(Exception, match="(?i)bundesagentur|503|upstream"):
        asyncio.run(collector_runner.run_bundesagentur(db, settings))
    db.rollback()
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 5


def test_known_sender_mail_for_two_applications_is_never_auto_cleared(db, monkeypatch):
    """Codex Stage 19 review round 3: one ATS sender, two applications; the
    operator moving one of them must not hide the mail for the other."""
    _collect(db, monkeypatch)
    jobs = _jobs(db)
    first = _submit_via_cli(db, jobs[JUNIOR])
    second = _submit_via_cli(db, jobs[QA])
    processor = ApplicationResponseProcessor()
    for job, key in ((jobs[JUNIOR], "a"), (jobs[QA], "b")):
        receipt = _mail(
            f"Ihre Bewerbung als {job.title}",
            f"Vielen Dank für Ihre Bewerbung als {job.title} bei {job.company}.",
            sender="karriere@shared-ats.example",
            key=f"r-{key}@ats",
            at=SUBMITTED_AT + timedelta(minutes=5),
        )
        processor.process(db, [receipt], apply=True, applicant_addresses=(APPLICANT,))
    vague = _mail(
        "Neuigkeiten",
        "Wir laden Sie gerne zu einem Vorstellungsgespräch ein.",
        sender="karriere@shared-ats.example",
        key="vague@ats",
        at=SUBMITTED_AT + timedelta(days=6),
    )
    [flagged] = processor.process(db, [vague], apply=True, applicant_addresses=(APPLICANT,))
    assert flagged.review_candidates == tuple(sorted((first.id, second.id)))
    path = db.get_bind().url.database
    db.commit()
    move = ["--database", path, "transition", str(first.id), "INTERVIEW"]
    assert tracker_cli.run([*move, "--actor", "operator", "--reason", "x", "--apply"]) == 0
    db.expire_all()
    [rescan] = processor.process(db, [vague], apply=True, applicant_addresses=(APPLICANT,))
    assert rescan.operator_review == NEEDS_OPERATOR_REVIEW


def test_stale_match_never_turns_a_senior_posting_into_not_suitable(db, api, monkeypatch):
    """Codex Stage 19 review round 3: without a current analysis the years
    in the wording alone send the posting to the operator."""
    _collect(db, monkeypatch)
    jobs, assessed = _ranked(db, api)
    job = jobs[SENIOR]
    job.description += " Wir freuen uns auf Sie."
    db.commit()
    after = fit.assess_job(db, job.id)
    assert after.criterion("skills").status == "NOT_EVALUATED"
    assert after.criterion("experience").status == "GAP"
    # The explicit C1 requirement (B1 confirmed) still makes it unsuitable,
    # but the wording-only experience gap is no longer one of the barriers.
    assert after.fit_tier == "NOT_SUITABLE"
    assert any("German C1" in r for r in after.tier_reasons)
    assert not any("experience GAP" in r for r in after.tier_reasons)
