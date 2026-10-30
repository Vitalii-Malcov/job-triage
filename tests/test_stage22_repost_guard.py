"""Stage 22 (P0-3): advisory duplicate signal for renamed / re-published vacancies.

Texts are shortened versions of the real public postings collected on
2026-10-30 (no personal data; contact persons removed): the ALD Vacuum
Technologies "Backend Developer" in Hanau was re-posted verbatim by N.O.C
Engineering and paraphrased by Weber GmbH and FERCHAU under other titles;
ALD renamed one of its postings. Application ids and statuses are synthetic.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.agents.description_similarity import (
    compare_descriptions,
    role_section_text,
)
from app.db import application_repository as app_repo
from app.db.models import ApplicationEventRecord, ApplicationRecord, Base, JobRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services import fit_cache
from app.services.application_duplicates import (
    SIMILAR_DESCRIPTION,
    VacancyView,
    assess_vacancy,
    classify_duplicate,
    verdict,
)
from app.services.application_guard import (
    applied_job_descriptions,
    ensure_not_already_applied,
    vacancy_findings,
)
from app.services.application_tracker import ApplicationTracker
from app.services.job_fit_assessment import duplicate_criterion

AT = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
BA = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"

ALD_INTRO = (
    "Als weltweit führender Hersteller von vakuumtechnischen Anlagen im Bereich Metallurgie "
    "und Wärmebehandlung beschäftigen wir ca. 1.000 Mitarbeiter in 9 Ländern. Eingebunden in "
    "ein börsennotiertes Konzernumfeld suchen wir für unser Headquarter in Hanau\n"
)
ALD_BENEFITS = (
    "\n### Auf was Sie sich bei uns freuen dürfen:\n"
    "- Ihr Start bei ALD: Umfangreiche Einarbeitung durch erfahrene Kollegen\n"
    "- Sonderzahlungen: Urlaubs-/Weihnachtsgeld, Beteiligung am Unternehmenserfolg, "
    "Warengutschein, Vermögenswirksame Leistungen\n"
    "- Familienservice: Kindergartenzuschuss, GesundheitsPlus, ALD-Academy\n"
)
ALD_BACKEND = (
    ALD_INTRO + "### In Ihrer neuen Tätigkeit erwarten Sie diese spannenden Aufgaben:\n"
    "- Erstellung von Microservices für das Speichern sowie Auswerten von Daten. Welche "
    "verschiedenste Quellen besitzen können: Kamera-Bilder, OPC-UA, Files, ...\n"
    "- Kommunikation und Zusammenarbeit mit dem Vertrieb sowie der Projektleitung.\n"
    "- Deployments remote oder vor Ort beim Kunden weltweit.\n"
    "- Weiterentwicklung unserer Bildverarbeitungslösung zur automatisierten "
    "Qualitätskontrolle mit OpenCV (Python)\n"
    "### Das bringen Sie mit:\n"
    "- Sie haben ein Studium im Bereich Informatik oder einen artverwandten Studiengang.\n"
    "- Erfahrungen in der objektorientierten Programmierung, agilen Softwareentwicklung "
    "und Code-Testing.\n"
    "- Im Umgang mit (nicht-)relationalen Datenbanken, Docker, Git und Linux sind Sie versiert.\n"
    "- Erfahrungen mit PostgreSQL und Python.\n" + ALD_BENEFITS
)
NOC_REPOST = (
    "###### Zusammenfassung\nErstellung von Microservices für das Speichern sowie Auswerten "
    "von Daten\n###### Beschreibung\n"
    "- Erstellung von Microservices für das Speichern sowie Auswerten von Daten. Welche "
    "verschiedenste Quellen besitzen können: Kamera-Bilder, OPC-UA, Files.\n"
    "- Kommunikation und Zusammenarbeit mit dem Vertrieb sowie der Projektleitung.\n"
    "- Deployments remote oder vor Ort beim Kunden Weltweit.\n"
    "- Weiterentwicklung unserer Bildverarbeitungslösung zur automatisierten "
    "Qualitätskontrolle mit OpenCV (Python)\n"
    "###### Anforderungen\n"
    "- Sie haben ein Studium im Bereich Informatik oder einen artverwandten Studiengang "
    "absolviert.\n- Erfahrungen mit PostgreSQL und Python.\n"
    "###### Was wir bieten\n- Umfangreiche Einarbeitung, Gleitzeitmodell, 30 Tage Urlaub\n"
)
WEBER_PARAPHRASE = (
    "Werden Sie Teil der WEBER GmbH und unterstützen Sie uns bei der Realisierung unserer "
    "Kundenprojekte in Hanau als Backend Developer (all genders) 25185\n"
    "Ihre Aufgaben:\n"
    "- Entwicklung von Microservices zur Speicherung und Auswertung von Daten aus "
    "verschiedenen Quellen, wie Kamerabildern, OPC-UA-Schnittstellen oder Files\n"
    "- Weiterentwicklung der Bildverarbeitungslösung zur automatisierten Qualitätskontrolle "
    "auf Basis von OpenCV (Python)\n"
    "- Durchführung von Deployments bei internationalen Kunden über Remote-Funktionen\n"
    "Ihr Profil:\n"
    "- Ein erfolgreich abgeschlossenes Studium im Bereich Informatik\n"
    "- Fundierte Erfahrung im DevOps-Bereich wie Docker-Container, Kubernetes, Git\n"
    "- Verhandlungssichere Deutschkenntnisse und gute Englischkenntnisse\n"
    "Wir bieten:\n- Unbefristeter Arbeitsvertrag, Urlaubsgeld\n"
)
FERCHAU_PARAPHRASE = (
    "Menschen und Technologien zu verbinden ist unser Anspruch bei FERCHAU.\n"
    "**Ihr Aufgabengebiet**\n"
    "* Entwicklung von Microservices zur Datenverarbeitung aus Quellen wie Kamera-Bildern, "
    "OPC-UA und Dateien\n"
    "* Weiterentwicklung einer Bildverarbeitungslösung zur automatisierten Qualitätskontrolle "
    "mit Python und OpenCV\n"
    "* Durchführung von Deployments remote sowie gelegentlich direkt beim Kunden vor Ort\n"
    "**Ihre Vorteile bei uns**\n* Unbefristeter Arbeitsvertrag * 30 Tage Urlaub\n"
    "**Das bringen Sie mit**\n"
    "* Kenntnisse in Python sowie idealerweise Erfahrung mit OpenCV\n"
    "* Erfahrung in der Entwicklung von Microservices\n"
    "* Sicherer Umgang mit PostgreSQL sowie Docker, Git und Linux\n"
)
ALD_PROCESS_ENGINEER = (
    ALD_INTRO + "### In Ihrer neuen Tätigkeit erwarten Sie diese spannenden Aufgaben:\n"
    "- Betreuung und Leitung von verfahrens- und anlagentechnischen Entwicklungsprojekten\n"
    "- Mitwirkung an der Neu- und Weiterentwicklung metallurgischer Anlagen\n"
    "- Entwicklung von Elektronenstrahl-Prozessen und Anlageninbetriebnahmen weltweit\n"
    "### Das bringen Sie mit:\n"
    "- Ein technisches Studium aus Elektrotechnik, Maschinenbau oder Materialwissenschaften\n"
    "- Erfahrungen mit Elektronenstrahlanlagen oder Vakuumanlagen\n" + ALD_BENEFITS
)
ALD_PROTOTYPING = (
    ALD_INTRO + "### In Ihrer neuen Tätigkeit erwarten Sie diese spannenden Aufgaben:\n"
    "- Rapid Prototyping & Feature-Entwicklung: Sie programmieren an der Anlagenbediensoftware, "
    "entwickeln neue Features und führen schnelle Modifikationen durch.\n"
    "- Schnittstellenkompetenz: Sie agieren als Bindeglied zwischen Software-Programmierung "
    "und Hardware-ExpertInnen und überführen die Prototypen in Serie.\n"
    "- Data Analytics & Mustererkennung: Sie werten komplexe Datensätze aus, um "
    "Systemzusammenhänge über längere Zeiträume sowie viele Prozesse hinweg abzubilden.\n"
    "- Konzeptionelle Vorarbeit: Prototypen und Algorithmen dokumentieren, damit die Logik "
    "effizient in die Produktionssoftware überführt werden kann.\n" + ALD_BENEFITS
)
ALD_PROTOTYPING_RENAMED = ALD_PROTOTYPING.replace(
    "an der Anlagenbediensoftware,",
    "an der Anlagenbediensoftware unserer additiven Fertigungssysteme (Powder Bed Fusion),",
)
# Two different roles of one employer that share the recruiter block.
SISTER_ROLE_A = (
    "Your role:\n- Develop automated test scripts for automotive control units in Python\n"
    "- Integrate test benches with HIL systems and CANoe\n"
    "Your profile:\n- Experience with pytest, CANoe, HIL test benches\n"
    "- Contact: Erika Mustermann, Beispiel recruiting team, apprentice programme\n"
)
SISTER_ROLE_B = (
    "Your role:\n- Support dealers in after-sales processes for vehicle diagnostics\n"
    "- Train workshop staff on warranty cases\n"
    "Your profile:\n- Automotive after-sales background, warranty handling\n"
    "- Contact: Erika Mustermann, Beispiel recruiting team, apprentice programme\n"
)


# --- pure text comparison -----------------------------------------------------------


def test_role_sections_drop_employer_intro_and_benefits():
    role = role_section_text(ALD_BACKEND)
    assert "opc-ua" in role and "opencv" in role
    for boilerplate in ("metallurgie", "warengutschein", "kindergartenzuschuss", "academy"):
        assert boilerplate not in role


@pytest.mark.parametrize(
    "repost", [NOC_REPOST, WEBER_PARAPHRASE, FERCHAU_PARAPHRASE], ids=["noc", "weber", "ferchau"]
)
def test_reposts_of_the_ald_backend_text_are_similar(repost):
    similarity = compare_descriptions(ALD_BACKEND, repost)
    assert similarity is not None and similarity.similar, similarity
    assert {"opc", "opencv", "kamera"} <= set(similarity.terms)


def test_renamed_posting_of_the_same_employer_is_similar():
    similarity = compare_descriptions(ALD_PROTOTYPING, ALD_PROTOTYPING_RENAMED)
    assert similarity is not None and similarity.similar and similarity.overlap >= 0.9


@pytest.mark.parametrize("other", [ALD_PROTOTYPING, ALD_PROCESS_ENGINEER])
def test_shared_employer_template_alone_is_not_similar(other):
    similarity = compare_descriptions(ALD_BACKEND, other)
    assert similarity is None or not similarity.similar


@pytest.mark.parametrize("missing", [None, "", "Backend Developer (m/w/d)", "Python, SQL."])
def test_missing_or_thin_description_is_never_compared(missing):
    assert compare_descriptions(ALD_BACKEND, missing) is None
    assert compare_descriptions(missing, ALD_BACKEND) is None


def test_markdown_escapes_do_not_split_words():
    text = (
        "Aufgaben:" + chr(10) + "- verfahrens" + chr(92) + "-technik, C+" + chr(92) + "+ und OPC-UA"
    )
    role = role_section_text(text)
    assert chr(92) not in role and "verfahrens-technik" in role and "c++" in role


# --- duplicate check (pure) ---------------------------------------------------------


def _application(app_id, company, role, *, job_id, status=S.REJECTED, ref=None, location="Hanau"):
    from app.models.application_lifecycle import Application

    return Application(
        id=app_id,
        candidate_profile_id=None,
        job_id=job_id,
        company_name=company,
        role_title=role,
        location=location,
        source="bundesagentur",
        source_reference=ref or f"ref-{app_id}",
        external_job_id=None,
        vacancy_fingerprint=None,
        application_channel=C.EMPLOYER_PORTAL,
        destination_url=None,
        employer_domains=(),
        review_package_id=None,
        current_status=status,
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


ALD_APP = _application(
    2, "ALD Vacuum Technologies GmbH", "Backend Developer (m/w/div.)", job_id=174
)
ALD_PROTO_APP = _application(
    3,
    "ALD Vacuum Technologies GmbH",
    "Software Engineer (m/w/div.) im Bereich Prototyping & Data Analytics",
    job_id=169,
)
APPLIED_TEXTS = {2: ALD_BACKEND, 3: ALD_PROTOTYPING}


def _view(company, title, description, *, job_id=900, location="Hanau", url=None):
    return VacancyView(job_id, "bundesagentur", company, title, url, None, location, description)


@pytest.mark.parametrize(
    ("company", "title", "description", "location"),
    [
        ("Weber GmbH", "Backend Developer (all genders) 25185", WEBER_PARAPHRASE, "Hanau"),
        (
            "FERCHAU GmbH Niederlassung Frankfurt",
            "Softwareentwickler (m/w/d) Bildverarbeitung & Microservices",
            FERCHAU_PARAPHRASE,
            "Frankfurt am Main",
        ),
    ],
)
def test_renamed_repost_by_another_company_is_an_advisory_finding(
    company, title, description, location
):
    vacancy = _view(company, title, description, location=location)
    without_text = assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP])
    assert without_text == []  # before Stage 22 these were NO_DUPLICATE_FOUND
    findings = assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP], APPLIED_TEXTS)
    assert [f.application_id for f in findings] == [2]
    (finding,) = findings
    assert finding.strength == "PROBABLE" and finding.signals == (SIMILAR_DESCRIPTION,)
    assert finding.advisory_only and finding.status == "REJECTED"
    assert "opencv" in finding.detail
    assert verdict(findings) == "POSSIBLE_DUPLICATE"
    assert classify_duplicate(vacancy, findings) == "POSSIBLE_DUPLICATE"


def test_verbatim_repost_with_same_title_keeps_title_signal_and_adds_text_signal():
    vacancy = _view("N.O.C Engineering GmbH", "Backend Developer (m/w/div.)", NOC_REPOST)
    (finding,) = assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP], APPLIED_TEXTS)
    assert finding.signals == ("SAME_TITLE_OTHER_EMPLOYER", SIMILAR_DESCRIPTION)
    assert finding.strength == "PROBABLE" and not finding.advisory_only


def test_renamed_posting_at_the_same_employer_is_raised_from_info_to_probable():
    vacancy = _view(
        "ALD Vacuum Technologies  GmbH",
        "Software Engineer (m/w/div.) Additive Manufacturing & Data Analytics",
        ALD_PROTOTYPING_RENAMED,
    )
    findings = {f.application_id: f for f in assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP])}
    assert findings[3].strength == "INFO"  # identifiers alone: other role
    findings = {
        f.application_id: f
        for f in assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP], APPLIED_TEXTS)
    }
    assert findings[3].strength == "PROBABLE" and findings[3].signals == (SIMILAR_DESCRIPTION,)
    assert findings[2].strength == "INFO"  # the Backend text is a different job


def test_sister_role_of_the_same_employer_stays_informational():
    vacancy = _view(
        "ALD Vacuum Technologies GmbH", "Prozessingenieur (m/w/div.)", ALD_PROCESS_ENGINEER
    )
    findings = assess_vacancy(vacancy, [ALD_APP, ALD_PROTO_APP], APPLIED_TEXTS)
    assert {f.strength for f in findings} == {"INFO"}
    assert all(SIMILAR_DESCRIPTION not in f.signals for f in findings)


def test_shared_recruiter_block_at_one_employer_is_not_similar_enough():
    applied = _application(18, "Beispiel Engineering GmbH", "Test Automation Engineer", job_id=1)
    vacancy = _view("Beispiel Engineering GmbH", "After Sales Specialist (m/f/d)", SISTER_ROLE_B)
    findings = assess_vacancy(vacancy, [applied], {18: SISTER_ROLE_A})
    assert [f.signals for f in findings] == [("SAME_EMPLOYER_OTHER_ROLE",)]


@pytest.mark.parametrize("description", [None, "", "Backend Developer"])
def test_vacancy_without_description_gets_no_text_signal(description):
    vacancy = _view("Weber GmbH", "Backend Developer (all genders) 25185", description)
    assert assess_vacancy(vacancy, [ALD_APP], APPLIED_TEXTS) == []


def test_application_without_job_row_gets_no_text_signal():
    orphan = _application(7, "ALD Vacuum Technologies GmbH", "Backend Developer", job_id=None)
    vacancy = _view("Weber GmbH", "Backend Developer (all genders) 25185", WEBER_PARAPHRASE)
    assert assess_vacancy(vacancy, [orphan], {}) == []


def test_identifier_signals_are_unchanged_and_stay_strong():
    vacancy = _view(
        "ALD Vacuum Technologies GmbH",
        "Backend Developer (m/w/div.)",
        ALD_BACKEND,
        job_id=174,
        url=BA + "ref-2",
    )
    findings = assess_vacancy(vacancy, [ALD_APP], APPLIED_TEXTS)
    assert findings[0].strength == "STRONG"
    assert SIMILAR_DESCRIPTION not in findings[0].signals and findings[0].detail == ""
    assert verdict(findings) == "ALREADY_APPLIED"


# --- database: guard, fit criterion, CLI, cache ----------------------------------------


def _job(db, job_id, company, title, description, *, location="Hanau", ref=None):
    db.add(
        JobRecord(
            id=job_id,
            fingerprint=f"{job_id:064d}",
            source="bundesagentur",
            title=title,
            company=company,
            location=location,
            url=BA + (ref or f"10001-{job_id}-S"),
            description=description,
            skills_json="[]",
            score=0,
            recommendation="SKIP",
            status="NEW",
            first_seen_at=AT,
            last_seen_at=AT,
        )
    )


def _seed(db):
    _job(
        db,
        174,
        "ALD Vacuum Technologies GmbH",
        "Backend Developer (m/w/div.)",
        ALD_BACKEND,
        ref="20385-2347902-S",
    )
    _job(db, 144, "Weber GmbH", "Backend Developer (all genders) 25185", WEBER_PARAPHRASE)
    _job(
        db,
        163,
        "FERCHAU GmbH Niederlassung Frankfurt",
        "Softwareentwickler (m/w/d) Bildverarbeitung & Microservices",
        FERCHAU_PARAPHRASE,
        location="Frankfurt am Main",
    )
    _job(
        db,
        2892,
        "N.O.C Engineering GmbH",
        "Backend Developer (m/w/div.)",
        NOC_REPOST,
        location="Offenbach am Main",
    )
    _job(
        db,
        500,
        "Example GmbH",
        "Data Analyst (m/w/d)",
        "Ihre Aufgaben:\n- Reporting mit Power BI, DAX und Fabric für Controlling\n"
        "- Dashboards für Vertrieb und Einkauf, Kennzahlen, Forecasts\nIhr Profil:\n- SQL",
    )
    db.flush()
    tracker = ApplicationTracker()
    app = tracker.create_application(
        db,
        company_name="ALD Vacuum Technologies GmbH",
        role_title="Backend Developer (m/w/div.)",
        source="bundesagentur",
        source_reference="20385-2347902-S",
        application_channel=C.EMPLOYER_PORTAL,
        initial_status=S.SUBMITTED,
        origin=O.OPERATOR,
        actor="test",
        occurred_at=AT - timedelta(days=13),
        job_id=174,
        correlation_key="submission:ald-backend",
    ).application
    tracker.transition_application(
        db, app.id, S.REJECTED, origin=O.OPERATOR, actor="test", reason="Absage 28.10."
    )
    db.commit()
    return app.id


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 's22.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _snapshot(db):
    apps = [tuple(r) for r in db.execute(select(ApplicationRecord.__table__)).all()]
    events = [tuple(r) for r in db.execute(select(ApplicationEventRecord.__table__)).all()]
    return apps, events


def test_guard_reports_reposts_but_never_refuses_them(db):
    app_id = _seed(db)
    before = _snapshot(db)
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(db)]
    assert applied_job_descriptions(db, applications) == {app_id: ALD_BACKEND}
    for job_id in (144, 163, 2892):
        job = db.get(JobRecord, job_id)
        findings = vacancy_findings(db, job)
        assert [f.application_id for f in findings] == [app_id]
        assert SIMILAR_DESCRIPTION in findings[0].signals and findings[0].strength == "PROBABLE"
        ensure_not_already_applied(db, job)  # advisory: no AlreadyAppliedError
        criterion = duplicate_criterion(db, job)
        assert criterion.status == "POSSIBLE_DUPLICATE"
        assert SIMILAR_DESCRIPTION in criterion.reason
    assert vacancy_findings(db, db.get(JobRecord, 500)) == []
    assert _snapshot(db) == before  # nothing written


def test_guard_ignores_an_application_whose_job_row_is_gone(db):
    _seed(db)
    db.execute(text("DELETE FROM jobs WHERE id = 174"))
    db.commit()
    assert vacancy_findings(db, db.get(JobRecord, 144)) == []


def _migrated(tmp_path):
    from tests.test_stage15_response_tracking import _migrated_db

    path = _migrated_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    _seed(session)
    counts = (
        session.scalar(select(func.count()).select_from(ApplicationRecord)),
        session.scalar(select(func.count()).select_from(ApplicationEventRecord)),
    )
    session.close()
    engine.dispose()
    return path, counts


def test_cli_check_job_prints_the_advisory_and_stays_read_only(tmp_path, capsys):
    import scripts.stage15_application_tracker as cli

    path, counts = _migrated(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert cli.run(["--database", str(path), "check-job", "144"]) == 0
    out = capsys.readouterr().out
    assert "VERDICT: POSSIBLE_DUPLICATE" in out and "SIMILAR_DESCRIPTION" in out
    assert "shared distinctive terms" in out
    assert cli.run(["--database", str(path), "--json", "check-job", "163"]) == 0
    out = capsys.readouterr().out
    assert '"SIMILAR_DESCRIPTION"' in out and '"detail": "' in out
    assert cli.run(["--database", str(path), "check-job", "500"]) == 0
    assert "NO_DUPLICATE_FOUND" in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert counts == (1, 2)


def _record(path, job_id, company, role, ref, *extra):
    import scripts.stage15_application_tracker as cli

    return cli.run(
        [
            "--database",
            str(path),
            "record-submission",
            "--actor",
            "operator",
            "--evidence",
            "test",
            "--company",
            company,
            "--role",
            role,
            "--source",
            "bundesagentur",
            "--reference",
            ref,
            "--channel",
            "EMAIL",
            "--submitted-at",
            "2026-10-30T09:00:00Z",
            "--job-id",
            str(job_id),
            *extra,
        ]
    )


def test_record_submission_prints_advisory_and_does_not_refuse(tmp_path, capsys):
    path, _ = _migrated(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    code = _record(path, 144, "Weber GmbH", "Backend Developer (all genders) 25185", "10001-144-S")
    out = capsys.readouterr().out
    assert code == 0 and "ADVISORY: application 1" in out and "DRY RUN" in out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_record_submission_still_refuses_the_title_mirror(tmp_path, capsys):
    path, _ = _migrated(tmp_path)
    code = _record(
        path, 2892, "N.O.C Engineering GmbH", "Backend Developer (m/w/div.)", "10001-2892-S"
    )
    captured = capsys.readouterr()
    assert code == 3 and "REFUSED" in captured.err
    assert "PROBABLE: application 1" in captured.out


def test_cache_key_of_other_vacancies_follows_applied_job_descriptions(db):
    _seed(db)
    before = fit_cache.current_keys(db)
    db.execute(
        text("UPDATE jobs SET description = :d WHERE id = 174"),
        {"d": ALD_BACKEND + "\n- Erfahrung mit Grafana"},
    )
    db.commit()
    after_edit = fit_cache.current_keys(db)
    assert all(after_edit[j] != before[j] for j in (144, 163, 500, 2892))
    db.execute(text("DELETE FROM jobs WHERE id = 174"))
    db.commit()
    after_delete = fit_cache.current_keys(db)
    assert all(after_delete[j] != after_edit[j] for j in (144, 163, 500, 2892))


def test_unrelated_description_change_only_invalidates_its_own_row(db):
    _seed(db)
    before = fit_cache.current_keys(db)
    db.execute(text("UPDATE jobs SET description = 'neu' WHERE id = 500"))
    db.commit()
    after = fit_cache.current_keys(db)
    assert after[500] != before[500]
    assert all(after[j] == before[j] for j in (144, 163, 174, 2892))


def test_applied_jobs_hash_is_stable_without_applications(db):
    _job(db, 1, "X GmbH", "Y", "z")
    db.commit()
    assert fit_cache.applied_jobs_hash(db) == fit_cache.applied_jobs_hash(db)


# --- Codex review round 1 ------------------------------------------------------------

GENERIC_DUTIES = (
    "Ihre Aufgaben:\n- Mitarbeit im Team, Abstimmung mit Fachbereichen, Dokumentation\n"
)
REQUIREMENTS_ONLY = (
    GENERIC_DUTIES + "Das bringen Sie mit:\n"
    "- Erfahrung mit Modbus, Profinet, Siemens TIA, WinCC, Zenon, Kepware und Ignition\n"
)


def test_requirements_under_das_bringen_sie_mit_are_compared():
    assert "zenon" in role_section_text(REQUIREMENTS_ONLY)
    similarity = compare_descriptions(REQUIREMENTS_ONLY, REQUIREMENTS_ONLY)
    assert similarity is not None and similarity.similar
    assert {"kepwar", "zenon", "wincc"} <= set(similarity.terms)


NL = chr(10)
SHARED_PROFILE = (
    "Ihr Profil:" + NL + "- Erfahrung mit Codesys, Beckhoff, TwinCAT, Hilscher, Kepware, Zenon, "
    "Ignition, WinCC und Profinet" + NL
)
SISTER_ROLE_WITH_SHARED_PROFILE = (
    "Ihre Aufgaben:" + NL + "- Planung von Rechenzentrumsumzügen, Verkabelung, Klimatisierung, "
    "Brandschutz, USV-Anlagen, Racks, Glasfaser, Patchfelder und Stromverteilung"
    + NL
    + SHARED_PROFILE
)


def test_employer_benefit_heading_with_bringen_is_not_a_role_section():
    text = (
        "Ihre Aufgaben:"
        + NL
        + "- Pflege der Kundendaten"
        + NL
        + "Was wir Ihnen bringen:"
        + NL
        + "- Jobrad, Kantine, Betriebskita, Sabbatical, Firmenwagen, Gleitzeitkonto"
        + NL
    )
    role = role_section_text(text)
    assert "kundendaten" in role and "jobrad" not in role and "sabbatical" not in role
    career = text.replace("Was wir Ihnen bringen:", "So bringen Sie Ihre Karriere mit uns voran:")
    role = role_section_text(career)
    assert "kundendaten" in role and "jobrad" not in role
    for heading in (
        "Das bringen Sie mit:",
        "Das bringen Sie idealerweise mit:",
        "Das bringen Sie dafür mit:",
        "Was du mitbringst:",
        "Das bringst du mit:",
        "Was bringst du idealerweise mit?",
    ):
        with_tasks = "Ihre Aufgaben:" + NL + "- Pflege" + NL + heading + NL + "- Zenon" + NL
        assert "zenon" in role_section_text(with_tasks), heading


def test_same_employer_shared_profile_is_not_a_rename():
    similarity = compare_descriptions(SHARED_PROFILE, SISTER_ROLE_WITH_SHARED_PROFILE)
    assert similarity is not None and similarity.overlap >= 0.9
    assert similarity.coverage_of_larger < 0.75
    applied = _application(30, "Beispiel Cloud GmbH", "Platform Engineer", job_id=1)
    vacancy = _view("Beispiel Cloud GmbH", "Data Center Planner", SISTER_ROLE_WITH_SHARED_PROFILE)
    findings = assess_vacancy(vacancy, [applied], {30: SHARED_PROFILE})
    assert [f.signals for f in findings] == [("SAME_EMPLOYER_OTHER_ROLE",)]


def test_merge_findings_keeps_the_strongest_and_all_signals():
    from app.services.application_duplicates import DuplicateFinding, merge_findings

    advisory = DuplicateFinding(1, "PROBABLE", (SIMILAR_DESCRIPTION,), "A", "R", "SUBMITTED", "d")
    strong = DuplicateFinding(1, "STRONG", ("SAME_URL",), "A", "R", "SUBMITTED")
    other = DuplicateFinding(2, "INFO", ("SAME_EMPLOYER_OTHER_ROLE",), "B", "S", "SUBMITTED")
    (first, second) = merge_findings([advisory, other], [strong])
    assert first.application_id == 1 and first.strength == "STRONG"
    assert first.signals == (SIMILAR_DESCRIPTION, "SAME_URL") and not first.advisory_only
    assert second == other


def test_record_submission_advisory_never_masks_a_strong_destination_url(tmp_path, capsys):
    path, _ = _migrated(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE applications SET destination_url = 'https://jobs.example.invalid/ald-1'")
        )
    engine.dispose()
    code = _record(
        path,
        144,
        "Weber GmbH",
        "Backend Developer (all genders) 25185",
        "10001-144-S",
        "--destination-url",
        "https://jobs.example.invalid/ald-1",
    )
    captured = capsys.readouterr()
    assert code == 3 and "REFUSED" in captured.err
    assert "STRONG: application 1" in captured.out and "SAME_URL" in captured.out
