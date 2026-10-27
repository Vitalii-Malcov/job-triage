"""Stage 15 Codex final review (verdict C) -- regression tests for every
reproduction:

A. forged rejection from an unrelated sender  -> no automatic REJECTED
B. forged offer from an unrelated sender      -> no automatic OFFER
C. canonical DB with a changed hash           -> path still refused
D. bulk UPDATE of application_events          -> rejected by the database
E. bulk DELETE of application_events          -> rejected by the database
F. SQLite parent DELETE with events           -> rejected (foreign keys ON)
G. named inline HTML attachment               -> not the message body
H. negated technical interview                -> INTERVIEW, not TECHNICAL

plus the terminal-path guards at the single lifecycle writer.
"""

import email
import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.orm import sessionmaker

import scripts.stage15_application_tracker as cli
import scripts.stage15_import_ald_application as bootstrap
from app.agents.application_response_classifier import classify_application_response
from app.db.base import Base
from app.db.models import ApplicationEventRecord, ApplicationRecord
from app.db.session_engine import _set_sqlite_foreign_keys, enable_sqlite_foreign_keys
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationEventType as T
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.application_lifecycle import ResponseClassification as R
from app.providers.email import imap as imap_module
from app.services.application_correlation import InboundMessage
from app.services.application_response_processor import ApplicationResponseProcessor
from app.services.application_tracker import (
    ApplicationInputError,
    ApplicationTracker,
    TerminalStatusNotPermittedError,
)
from tests import stage15_fixtures as fx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = PROJECT_ROOT / "stage14b_canonical_v12.db"
CANONICAL_SHA = "874d2cab6fc78efe3574dd1fb123c06e60662da48c32c240227313778f41e35d"


def _engine(path: Path):
    return enable_sqlite_foreign_keys(create_engine(f"sqlite:///{path}"))


@pytest.fixture()
def db(tmp_path):
    engine = _engine(tmp_path / "remediation.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _ald(db):
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name="ALD Vacuum Technologies GmbH",
            role_title="Backend Developer (m/w/div.)",
            source="bundesagentur",
            source_reference="20385-2347902-S",
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=S.SUBMITTED,
            origin=O.IMPORT,
            actor="test",
            occurred_at=fx.ALD_SUBMITTED_AT,
            employer_domains=("ald-vt.de",),
            correlation_key="portal-submission:ald",
        )
        .application
    )


def _forged(body: str, key: str) -> InboundMessage:
    return InboundMessage(
        account_key="applicant@example.invalid",
        message_key=key,
        message_id_header=f"<{key}>",
        reply_to_ids=(),
        from_address="news@unrelated.example",
        from_display_name="Newsletter",
        to_addresses=("applicant@example.invalid",),
        subject="Regarding ALD Vacuum Technologies GmbH",
        body=body,
        has_html=False,
        occurred_at=fx.ALD_RECEIPT_ARRIVAL,
    )


# --- A/B. forged terminal mail ------------------------------------------------------


def test_A_forged_rejection_never_rejects(db):
    ald = _ald(db)
    message = _forged(
        "Reference 20385-2347902-S.\nWe regret to inform you that your application was rejected.",
        "mid:forged-rejection@unrelated.example",
    )
    for apply in (False, True):
        result = ApplicationResponseProcessor().process(db, [message], apply=apply)[0]
        assert result.classification == R.REJECTION.value
        assert result.correlation_confidence == "HIGH"  # company + public reference
        assert result.transition_applied is False
        assert result.operator_confirmation_required is True
        assert result.proposed_status == "REJECTED"
        assert result.reason == "TERMINAL_EMAIL_REQUIRES_OPERATOR_CONFIRMATION"
    app = ApplicationTracker().get_application(db, ald.id)
    assert app.current_status == S.SUBMITTED  # UNCHANGED
    history = ApplicationTracker().get_application_history(db, ald.id)
    assert [e.event_type for e in history] == [T.CREATED, T.INBOUND_MESSAGE]
    evidence = history[1]
    assert evidence.new_status is None and evidence.origin == O.GMAIL
    assert evidence.metadata["operator_confirmation_required"] is True
    assert evidence.metadata["proposed_status"] == "REJECTED"


def test_B_forged_offer_never_offers(db):
    ald = _ald(db)
    message = _forged(
        "Reference 20385-2347902-S.\nWe are pleased to offer you the position. Job offer attached.",
        "mid:forged-offer@unrelated.example",
    )
    result = ApplicationResponseProcessor().process(db, [message], apply=True)[0]
    assert result.classification == R.OFFER.value
    assert not result.transition_applied and result.operator_confirmation_required
    assert result.proposed_status == "OFFER"
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.SUBMITTED


def test_genuine_looking_terminal_mail_also_needs_the_operator(db):
    ald = _ald(db)
    real_sender = InboundMessage(
        account_key="applicant@example.invalid",
        message_key="mid:rej@ald-vt.de",
        message_id_header="<rej@ald-vt.de>",
        reply_to_ids=(),
        from_address="karriere@ald-vt.de",
        from_display_name="ALD Vacuum Technologies GmbH",
        to_addresses=("applicant@example.invalid",),
        subject="Ihre Bewerbung als Backend Developer",
        body="Leider müssen wir Ihre Bewerbung absagen.",
        has_html=False,
        occurred_at=fx.ALD_RECEIPT_ARRIVAL,
    )
    result = ApplicationResponseProcessor().process(db, [real_sender], apply=True)[0]
    assert result.correlation_confidence == "HIGH" and not result.transition_applied
    assert result.operator_confirmation_required
    assert ApplicationTracker().get_application(db, ald.id).current_status == S.SUBMITTED


def test_weak_terminal_mail_still_does_nothing(db):
    _ald(db)
    weak = _forged("We regret to inform you that you have not been selected.", "mid:weak@x")
    weak = InboundMessage(**{**weak.__dict__, "subject": "Hello"})
    result = ApplicationResponseProcessor().process(db, [weak], apply=True)[0]
    assert result.event_recorded is False and result.transition_applied is False
    assert db.scalar(select(func.count()).select_from(ApplicationEventRecord)) == 1


@pytest.mark.parametrize("origin", [O.GMAIL, O.SYSTEM, O.PORTAL, O.IMPORT])
@pytest.mark.parametrize("target", [S.REJECTED, S.OFFER, S.WITHDRAWN])
def test_single_writer_refuses_terminal_status_from_non_operator(db, origin, target):
    ald = _ald(db)
    tracker = ApplicationTracker()
    if target == S.OFFER:
        tracker.transition_application(
            db, ald.id, S.INTERVIEW, origin=O.GMAIL, actor="p", correlation_key="gmail:i"
        )
    with pytest.raises(TerminalStatusNotPermittedError):
        tracker.transition_application(
            db, ald.id, target, origin=origin, actor="x", reason="because"
        )
    assert tracker.get_application(db, ald.id).current_status != target


def test_operator_terminal_transition_requires_a_reason(db):
    ald = _ald(db)
    tracker = ApplicationTracker()
    with pytest.raises(ApplicationInputError, match="reason is required"):
        tracker.transition_application(db, ald.id, S.WITHDRAWN, origin=O.OPERATOR, actor="op")
    outcome = tracker.transition_application(
        db, ald.id, S.WITHDRAWN, origin=O.OPERATOR, actor="op", reason="Accepted another offer"
    )
    assert outcome.event.origin == O.OPERATOR and outcome.event.reason


def test_override_is_always_operator_and_creation_never_terminal(db):
    import inspect as pyinspect

    assert "origin" not in pyinspect.signature(ApplicationTracker.override_status).parameters
    with pytest.raises(TerminalStatusNotPermittedError):
        ApplicationTracker().create_application(
            db,
            company_name="X",
            role_title="Y",
            source="s",
            source_reference="r",
            application_channel=C.OTHER,
            initial_status=S.REJECTED,
            origin=O.OPERATOR,
            actor="op",
        )


def test_lifecycle_status_has_exactly_one_writer():
    writers = []
    for root in ("app", "scripts"):
        for path in (PROJECT_ROOT / root).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "cas_update_application(" in source or "insert_application(" in source:
                writers.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert sorted(writers) == [
        "app/db/application_repository.py",
        "app/services/application_tracker.py",
    ]


# --- C. canonical path guard ----------------------------------------------------------


def _migrated(path: Path) -> Path:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    upgrade(cfg, "head")
    return path


@pytest.fixture()
def fake_canonical(tmp_path, monkeypatch):
    """A migrated DB standing in for the canonical file (its path is the
    protected identity; its bytes are NOT the historical canonical bytes)."""
    (tmp_path / "canon").mkdir()
    path = _migrated(tmp_path / "canon" / "stage14b_canonical_v12.db")
    monkeypatch.setattr(cli, "PROTECTED_DATABASE_PATHS", (path,))
    return path


def _refused(capsys, *args) -> bool:
    code = cli.run(["--database", *args, "list"])
    err = capsys.readouterr().err
    return code == 2 and "protected Stage 14B canonical" in err


def test_C_canonical_path_refused_after_its_hash_changed(fake_canonical, capsys):
    assert (
        hashlib.sha256(fake_canonical.read_bytes()).hexdigest() not in cli.PROTECTED_DATABASE_SHA256
    )
    with fake_canonical.open("ab") as handle:
        handle.write(b"\0" * 16)  # contents (and SHA) changed again
    assert _refused(capsys, str(fake_canonical))


def test_C_canonical_path_spellings_are_all_refused(fake_canonical, capsys, monkeypatch):
    monkeypatch.chdir(fake_canonical.parent.parent)
    spellings = [
        str(fake_canonical),
        "canon/stage14b_canonical_v12.db",
        "canon\\stage14b_canonical_v12.db",
        str(fake_canonical).replace("\\", "/"),
        f"canon{os.sep}..{os.sep}canon{os.sep}stage14b_canonical_v12.db",
        "./canon/./stage14b_canonical_v12.db",
    ]
    if os.name == "nt":
        spellings += [str(fake_canonical).upper(), "CANON\\STAGE14B_CANONICAL_V12.DB"]
    for spelling in spellings:
        assert _refused(capsys, spelling), spelling


def test_C_backslash_spelling_is_checked_as_a_separator_on_every_os():
    """Linux CI baseline failure: on POSIX `\\` is a literal file-name
    character, so `canon\\x.db` only reached "file does not exist". Both OS
    conventions are exercised here regardless of the host OS."""
    backslashed = Path("canon\\stage14b_canonical_v12.db")
    # Windows convention: `\\` is already a separator, nothing to add.
    assert cli._spellings(backslashed, sep="\\") == (backslashed,)
    # POSIX convention: the `/` reading is checked as well.
    posix = cli._spellings(backslashed, sep="/")
    assert posix[0] == backslashed
    assert posix[-1] == Path("canon/stage14b_canonical_v12.db")


def test_C_backslash_spelling_refused_before_any_engine(fake_canonical, capsys, monkeypatch):
    monkeypatch.chdir(fake_canonical.parent.parent)

    def _forbidden(*args, **kwargs):
        raise AssertionError("engine created for the protected canonical database")

    monkeypatch.setattr(cli, "create_engine", _forbidden)
    for spelling in ("canon\\stage14b_canonical_v12.db", ".\\canon\\.\\stage14b_canonical_v12.db"):
        assert _refused(capsys, spelling), spelling


def test_C_canonical_aliases_are_refused(fake_canonical, capsys, tmp_path):
    hard = tmp_path / "hardlink.db"
    os.link(fake_canonical, hard)
    assert _refused(capsys, str(hard))
    symlink = tmp_path / "symlink.db"
    try:
        os.symlink(fake_canonical, symlink)
    except (OSError, NotImplementedError):
        symlink = None
    if symlink is not None:
        assert _refused(capsys, str(symlink))
    if os.name == "nt":
        junction = tmp_path / "junction"
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(fake_canonical.parent)],
            capture_output=True,
        )
        if made.returncode == 0:
            assert _refused(capsys, str(junction / "stage14b_canonical_v12.db"))


def test_C_historical_sha_still_protects_byte_copies(tmp_path, monkeypatch, capsys):
    copy = _migrated(tmp_path / "other.db")
    monkeypatch.setattr(
        cli, "PROTECTED_DATABASE_SHA256", frozenset({hashlib.sha256(copy.read_bytes()).hexdigest()})
    )
    assert _refused(capsys, str(copy))


def test_C_ordinary_database_is_still_accepted(fake_canonical, tmp_path, capsys):
    other = _migrated(tmp_path / "runtime.db")
    assert cli.run(["--database", str(other), "list"]) == 0
    assert bootstrap.run(["--database", str(fake_canonical), "--receipt-source", "none"]) == 2


@pytest.mark.skipif(not CANONICAL.exists(), reason="local canonical DB not present")
def test_C_real_canonical_path_is_refused_without_being_written(capsys, monkeypatch):
    before = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    monkeypatch.chdir(PROJECT_ROOT)
    for spelling in (
        str(CANONICAL),
        "stage14b_canonical_v12.db",
        "./tests/../stage14b_canonical_v12.db",
    ):
        assert _refused(capsys, spelling)
    assert hashlib.sha256(CANONICAL.read_bytes()).hexdigest() == before == CANONICAL_SHA


# --- D/E. database-level append-only ------------------------------------------------


@pytest.fixture(params=["create_all", "alembic"])
def schema_db(request, tmp_path):
    path = tmp_path / f"immut_{request.param}.db"
    if request.param == "alembic":
        _migrated(path)
        engine = _engine(path)
    else:
        engine = _engine(path)
        Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    _ald(session)
    yield session
    session.close()
    engine.dispose()


def test_D_bulk_update_of_events_is_rejected(schema_db):
    for statement in (
        update(ApplicationEventRecord).values(reason="rewritten"),
        text("UPDATE application_events SET new_status = 'REJECTED'"),
    ):
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            schema_db.execute(statement)
            schema_db.commit()
        schema_db.rollback()
    event_row = schema_db.scalars(select(ApplicationEventRecord)).one()
    assert event_row.reason != "rewritten" and event_row.new_status == "SUBMITTED"


def test_E_bulk_delete_of_events_is_rejected(schema_db):
    for statement in (delete(ApplicationEventRecord), text("DELETE FROM application_events")):
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            schema_db.execute(statement)
            schema_db.commit()
        schema_db.rollback()
    assert schema_db.scalar(select(func.count()).select_from(ApplicationEventRecord)) == 1


def test_orm_update_and_delete_still_rejected(schema_db):
    from app.db.models import ApplicationEventImmutableError

    row = schema_db.scalars(select(ApplicationEventRecord)).one()
    row.reason = "x"
    with pytest.raises(ApplicationEventImmutableError):
        schema_db.flush()
    schema_db.rollback()
    schema_db.delete(schema_db.scalars(select(ApplicationEventRecord)).one())
    with pytest.raises(ApplicationEventImmutableError):
        schema_db.flush()
    schema_db.rollback()


# --- F. SQLite foreign keys ---------------------------------------------------------


def test_F_parent_delete_with_events_is_rejected(schema_db):
    for statement in (delete(ApplicationRecord), text("DELETE FROM applications")):
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            schema_db.execute(statement)
            schema_db.commit()
        schema_db.rollback()
    assert schema_db.scalar(select(func.count()).select_from(ApplicationRecord)) == 1
    orphans = schema_db.scalar(
        text(
            "SELECT COUNT(*) FROM application_events e LEFT JOIN applications a "
            "ON a.id = e.application_id WHERE a.id IS NULL"
        )
    )
    assert orphans == 0


def test_F_project_engines_enforce_foreign_keys(tmp_path):
    import app.db.session as session_module

    assert event.contains(session_module.engine, "connect", _set_sqlite_foreign_keys) or (
        session_module.engine.dialect.name != "sqlite"
    )
    path = _migrated(tmp_path / "cli_fk.db")
    engine = cli.resolve_engine(str(path), None)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    engine.dispose()
    plain = create_engine(f"sqlite:///{path}")
    enable_sqlite_foreign_keys(plain)
    enable_sqlite_foreign_keys(plain)  # idempotent
    with plain.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    plain.dispose()


# --- G. HTML attachments --------------------------------------------------------------


REJECTION_HTML = "<p>We regret to inform you that your application was rejected.</p>"


def _html_part(body: str, *, disposition: str | None = None, filename: str | None = None):
    part = MIMEText(body, "html", "utf-8")
    if disposition:
        part.add_header(
            "Content-Disposition", disposition, **({"filename": filename} if filename else {})
        )
    elif filename:
        part.set_param("name", filename)
    return part


def _message(*parts) -> email.message.Message:
    outer = MIMEMultipart("mixed")
    for part in parts:
        outer.attach(part)
    return email.message_from_bytes(outer.as_bytes())


def test_G_named_inline_html_attachment_is_not_the_body():
    message = _message(
        MIMEText("Danke für Ihre Nachricht.", "plain", "utf-8"),
        _html_part(REJECTION_HTML, disposition="inline", filename="old-rejection.html"),
    )
    assert imap_module._extract_html_text(message) == ""
    html_only = _message(
        _html_part(REJECTION_HTML, disposition="inline", filename="old-rejection.html")
    )
    assert imap_module._extract_html_text(html_only) == ""
    assert classify_application_response("Info", "", None).classification == R.UNKNOWN


def test_G_html_attachment_disposition_is_ignored():
    message = _message(_html_part(REJECTION_HTML, disposition="attachment", filename="a.html"))
    assert imap_module._extract_html_text(message) == ""
    unnamed_attachment = _message(_html_part(REJECTION_HTML, disposition="attachment"))
    assert imap_module._extract_html_text(unnamed_attachment) == ""


def test_G_name_parameter_without_disposition_is_ignored():
    message = _message(_html_part(REJECTION_HTML, filename="old-rejection.html"))
    assert imap_module._extract_html_text(message) == ""


def test_G_genuine_html_bodies_are_still_extracted():
    html_only = email.message_from_bytes(fx.ald_receipt_mime())
    assert "deren Eingang wir hiermit bestätigen" in imap_module._extract_html_text(html_only)
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("Plain fallback", "plain", "utf-8"))
    alternative.attach(MIMEText("<p>Wir haben Ihre Bewerbung erhalten.</p>", "html", "utf-8"))
    parsed = email.message_from_bytes(alternative.as_bytes())
    assert imap_module._extract_html_text(parsed) == "Wir haben Ihre Bewerbung erhalten."
    inline_unnamed = _message(
        _html_part("<p>Einladung zum Vorstellungsgespräch</p>", disposition="inline")
    )
    assert imap_module._extract_html_text(inline_unnamed) == "Einladung zum Vorstellungsgespräch"


# --- H. negated technical interview --------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "We would like to invite you to an interview. This is not a technical interview.",
            R.INTERVIEW_INVITATION,
        ),
        (
            "Wir möchten Sie zu einem Vorstellungsgespräch einladen. "
            "Es handelt sich nicht um ein technisches Gespräch.",
            R.INTERVIEW_INVITATION,
        ),
        ("Wir möchten Sie zu einem Interview einladen. Kein Fachgespräch.", R.INTERVIEW_INVITATION),
        ("Wir laden Sie zu einem technischen Interview ein.", R.TECHNICAL_INTERVIEW_INVITATION),
        (
            "Wir möchten Sie zu einem technischen Interview einladen.",
            R.TECHNICAL_INTERVIEW_INVITATION,
        ),
        (
            "Wir laden Sie zu einem technischen Interview ein. Leider nicht vor 10 Uhr.",
            R.TECHNICAL_INTERVIEW_INVITATION,
        ),
        (
            "We would like to invite you to an interview. Parking is not available, "
            "it will be a technical interview with live coding.",
            R.TECHNICAL_INTERVIEW_INVITATION,
        ),
        ("Wir laden Sie nicht zu einem technischen Interview ein.", R.UNKNOWN),
    ],
)
def test_H_technical_marker_negation_is_clause_scoped(body, expected):
    assert classify_application_response("", body, None).classification == expected


# --- preserved behaviour ----------------------------------------------------------------


def test_ald_receipt_still_moves_submitted_to_received(db):
    ald = _ald(db)
    receipt = InboundMessage(
        account_key="applicant@example.invalid",
        message_key="mid:ald-receipt@ald-vt.de",
        message_id_header="<ald-receipt@ald-vt.de>",
        reply_to_ids=(),
        from_address=fx.ALD_RECEIPT_FROM,
        from_display_name=fx.ALD_RECEIPT_DISPLAY_NAME,
        to_addresses=("applicant@example.invalid",),
        subject=fx.ALD_RECEIPT_SUBJECT,
        body=fx.ALD_RECEIPT_TEXT,
        has_html=False,
        occurred_at=fx.ALD_RECEIPT_ARRIVAL,
    )
    first = ApplicationResponseProcessor().process(db, [receipt], apply=True)[0]
    again = ApplicationResponseProcessor().process(db, [receipt], apply=True)[0]
    assert first.transition_applied and first.new_status == "RECEIVED"
    assert again.duplicate and again.reason == "ALREADY_PROCESSED"
    assert len(ApplicationTracker().get_application_history(db, ald.id)) == 2


def test_canonical_db_is_untouched_by_this_module():
    if CANONICAL.exists():
        assert hashlib.sha256(CANONICAL.read_bytes()).hexdigest() == CANONICAL_SHA
    assert shutil is not None and datetime.now(UTC)
