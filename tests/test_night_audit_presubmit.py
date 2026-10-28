"""Night audit 2026-10-27: pre-submit package validation.

Synthetic packages with anonymized contact data (example.invalid, a 0151
dummy number). Reproduces the live incidents: another vacancy's CV in the
upload slot, a portal form requiring documents the candidate does not have,
documents built from an older profile, over-claimed German.
"""

import shutil

import pytest

from app.services.pre_submit_validation import (
    Expectation,
    pdf_page_count,
    validate_package,
    write_manifest,
)
from scripts import presubmit_check

EMAIL = "max.muster@example.invalid"
PHONE = "+49 151 00000000"


FILLER = "Berufserfahrung, Projekte und Weiterbildung im IT-Umfeld. " * 6


def _esc(line: str) -> bytes:
    text = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("latin-1", "replace")


def _pdf(pages: int, lines=(), *, filler: bool = True) -> bytes:
    """A small but real PDF with a Helvetica text layer pypdf can read."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    number, kids = 4, []
    for index in range(pages):
        page_lines = list(lines) if index == 0 else []
        if filler:
            page_lines += [FILLER[i : i + 70] for i in range(0, len(FILLER), 70)]
        stream = (
            b"BT /F1 9 Tf 11 TL 40 800 Td "
            + b" ".join(b"(" + _esc(line) + b") '" for line in page_lines)
            + b" ET"
        )
        objects[number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (number + 1)
        )
        objects[number + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        kids.append(number)
        number += 2
    objects[2] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % k for k in kids)
        + b"] /Count %d >>" % pages
    )
    out, offsets = b"%PDF-1.7\n", {}
    for key in sorted(objects):
        offsets[key] = len(out)
        out += b"%d 0 obj\n" % key + objects[key] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % number
    out += b"".join(b"%010d 00000 n \n" % offsets[k] for k in range(1, number))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (number, xref)
    return out


def _lines(html_text: str) -> list[str]:
    import re

    parts = re.split(r"</p>|<br\s*/?>", html_text)
    return [" ".join(re.sub(r"<[^>]+>", " ", part).split()) for part in parts if part.strip()]


def _package(root, name, *, company="Example GmbH", role="Softwaretester (m/w/d)", **text):
    package = root / name
    (package / "_quelle_html").mkdir(parents=True)
    contact = text.get("contact", f"{EMAIL} · {PHONE}")
    cv = text.get("cv", f"<html><body><p>Max Muster</p><p>{contact}</p></body></html>")
    letter = text.get(
        "letter",
        f"<html><body><p>{contact}</p><p>Bewerbung als {role} bei {company}</p></body></html>",
    )
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(_pdf(2, [name, *_lines(cv)]))
    (package / "Max_Muster_Anschreiben.pdf").write_bytes(_pdf(1, [name, *_lines(letter)]))
    (package / "_quelle_html" / "_cv.html").write_text(cv, encoding="utf-8")
    (package / "_quelle_html" / "_letter.html").write_text(letter, encoding="utf-8")
    return package


def _expect(**overrides):
    values = dict(
        company="Example GmbH",
        role="Softwaretester (m/w/d)",
        contact_email=EMAIL,
        contact_phone=PHONE,
        german_level="B1",
    )
    values.update(overrides)
    return Expectation(**values)


def _codes(report, severity=None):
    return {f.code for f in report.findings if severity is None or f.severity == severity}


def test_clean_package_with_matching_upload_copy_is_ready(tmp_path):
    package = _package(tmp_path, "example_tester")
    upload = tmp_path / "upload"
    upload.mkdir()
    copy = shutil.copy(package / "Max_Muster_Lebenslauf.pdf", upload / "cv.pdf")
    write_manifest(package, 5)
    report = validate_package(
        package, _expect(letter_required=False), attached={"cv": copy}, current_profile_version=5
    )
    assert report.outcome == "READY", report.findings
    assert {"CV_PDF_OK", "CV_TEXT_OK", "CV_ATTACHMENT_MATCHES"} <= _codes(report, "OK")


def test_cv_from_another_vacancy_package_is_blocked(tmp_path):
    """Live incident: the previous vacancy's CV was pre-filled in the form."""
    target = _package(tmp_path, "sample_tester")
    other = _package(tmp_path, "other_support", company="Other AG", role="Support (m/w/d)")
    report = validate_package(
        target,
        _expect(company="Sample GmbH", letter_required=False),
        attached={"cv": other / "Max_Muster_Lebenslauf.pdf"},
        sibling_packages=[target, other],
    )
    assert report.outcome == "BLOCKED"
    assert "CV_WRONG_PACKAGE" in _codes(report, "BLOCK")


def test_modified_upload_copy_is_blocked(tmp_path):
    package = _package(tmp_path, "example_tester")
    changed = tmp_path / "changed.pdf"
    changed.write_bytes(_pdf(2, ["edited"]))
    report = validate_package(package, _expect(), attached={"cv": changed})
    assert "CV_ATTACHMENT_MISMATCH" in _codes(report, "BLOCK")


def test_required_portal_fields_without_data_block(tmp_path):
    """Live case: the portal demanded a grade and a transcript that the
    posting never mentioned and the candidate has not confirmed."""
    package = _package(tmp_path, "example_devops", role="DevOps Engineer (m/w/d)")
    report = validate_package(
        package,
        _expect(role="DevOps Engineer (m/w/d)"),
        missing_required_fields=["Zeugnisnote", "Letzter Notenauszug"],
    )
    assert report.outcome == "BLOCKED"
    details = [f.detail for f in report.findings if f.code == "FORM_FIELD_NEEDS_CANDIDATE_INPUT"]
    assert len(details) == 2 and "Zeugnisnote" in details[0]


@pytest.mark.parametrize(
    ("contact", "code"),
    [
        (f"old.address@example.invalid · {PHONE}", "CV_CONTACT_EMAIL"),
        (f"{EMAIL} · +49 151 99999999", "CV_CONTACT_PHONE"),
    ],
)
def test_wrong_contact_data_blocks(tmp_path, contact, code):
    package = _package(tmp_path, "example_tester", contact=contact)
    assert code in _codes(validate_package(package, _expect()), "BLOCK")


def test_phone_formatting_is_tolerated(tmp_path):
    package = _package(tmp_path, "example_tester", contact=f"{EMAIL} · 0151 / 000 000 00")
    assert "CV_CONTACT_PHONE" not in _codes(validate_package(package, _expect()))


@pytest.mark.parametrize(
    "claim", ["Deutsch: C1", "Verhandlungssichere Deutschkenntnisse", "Muttersprache Deutsch"]
)
def test_german_overclaim_blocks(tmp_path, claim):
    cv = f"<p>{EMAIL} {PHONE}</p><p>Sprachen: {claim}</p>"
    package = _package(tmp_path, "example_tester", cv=cv)
    assert "CV_GERMAN_OVERCLAIM" in _codes(validate_package(package, _expect()), "BLOCK")


def test_truthful_german_level_is_fine(tmp_path):
    cv = f"<p>{EMAIL} {PHONE}</p><p>Deutsch B1 (B2-Kurs läuft)</p>"
    package = _package(tmp_path, "example_tester", cv=cv)
    assert "CV_GERMAN_OVERCLAIM" not in _codes(validate_package(package, _expect()))


def test_unconfirmed_claim_blocks(tmp_path):
    cv = f"<p>{EMAIL} {PHONE}</p><p>3 Jahre kommerzielle Erfahrung in Testautomatisierung</p>"
    package = _package(tmp_path, "example_tester", cv=cv)
    report = validate_package(package, _expect(unconfirmed_claims=("kommerzielle Erfahrung",)))
    assert "CV_UNCONFIRMED_CLAIM" in _codes(report, "BLOCK")


def test_letter_for_another_company_blocks(tmp_path):
    package = _package(tmp_path, "example_tester", company="Other AG")
    assert "LETTER_COMPANY" in _codes(validate_package(package, _expect()), "BLOCK")


def test_letter_for_another_role_needs_operator(tmp_path):
    package = _package(tmp_path, "example_tester", role="Vertriebsmitarbeiter")
    assert "LETTER_ROLE" in _codes(validate_package(package, _expect()), "WARN")


def test_broken_or_long_pdfs(tmp_path):
    package = _package(tmp_path, "example_tester")
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(b"%PDF-1.7 truncated")
    (package / "Max_Muster_Anschreiben.pdf").write_bytes(_pdf(3))
    report = validate_package(package, _expect())
    assert "CV_UNREADABLE" in _codes(report, "BLOCK")
    assert "LETTER_TOO_MANY_PAGES" in _codes(report, "WARN")


def test_pdf_without_text_layer_needs_operator(tmp_path):
    """A scanned / image-only PDF cannot be content-checked: a human must."""
    package = _package(tmp_path, "example_tester")
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(_pdf(2, filler=False))
    report = validate_package(package, _expect(letter_required=False))
    assert "CV_TEXT_UNVERIFIED" in _codes(report, "WARN")
    assert report.outcome == "NEEDS_OPERATOR"


@pytest.mark.parametrize(
    ("manifest", "code", "severity"),
    [
        (None, "PROFILE_VERSION_UNRECORDED", "WARN"),
        ('{"profile_version": 4}', "PROFILE_VERSION_STALE", "BLOCK"),
    ],
)
def test_profile_version(tmp_path, manifest, code, severity):
    package = _package(tmp_path, "example_tester")
    if manifest:
        (package / "_manifest.json").write_text(manifest, encoding="utf-8")
    report = validate_package(package, _expect(), current_profile_version=5)
    assert code in _codes(report, severity)


def test_current_profile_version_passes(tmp_path):
    package = _package(tmp_path, "example_tester")
    (package / "_manifest.json").write_text('{"profile_version": 5}', encoding="utf-8")
    report = validate_package(package, _expect(), current_profile_version=5)
    assert not _codes(report) & {"PROFILE_VERSION_STALE", "PROFILE_VERSION_UNRECORDED"}


def test_page_count_helper():
    assert pdf_page_count(_pdf(2)) == 2
    assert pdf_page_count(b"not a pdf") is None


def test_findings_never_contain_contact_values(tmp_path):
    package = _package(tmp_path, "example_tester", contact="nobody@example.invalid +49 1")
    rendered = repr(validate_package(package, _expect()).findings)
    assert EMAIL not in rendered and "151" not in rendered


def test_cli_refuses_missing_package(tmp_path, capsys):
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    code = presubmit_check.run(
        [
            "--database",
            str(db),
            "--package",
            str(tmp_path / "nope"),
            "--company",
            "C",
            "--role",
            "R",
        ]
    )
    assert code == 2 and "not a directory" in capsys.readouterr().err


def test_cli_rejects_bad_attach_syntax(tmp_path, capsys):
    package = _package(tmp_path, "example_tester")
    code = presubmit_check.run(
        ["--database", "x.db", "--package", str(package), "--company", "C", "--role", "R"]
        + ["--attach", "photo=a.jpg"]
    )
    assert code == 2 and "--attach" in capsys.readouterr().err


# --- Codex night-audit review P1: the PDF itself, not only its source ---


def test_without_the_upload_copy_never_ready(tmp_path):
    package = _package(tmp_path, "example_tester")
    report = validate_package(package, _expect(letter_required=False))
    assert report.outcome == "NEEDS_OPERATOR"
    assert "UPLOAD_NOT_CHECKED" in _codes(report, "WARN")


def test_content_is_read_from_the_pdf_not_the_html(tmp_path):
    """Codex round 2: another person's CV copied into the package BEFORE
    binding, the HTML source left untouched, an exact copy attached."""
    package = _package(tmp_path, "example_tester")
    stranger = _pdf(2, ["Erika Beispiel", "erika@example.invalid · +49 160 11111111"])
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(stranger)
    write_manifest(package, 5)
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(stranger)
    report = validate_package(
        package, _expect(letter_required=False), attached={"cv": copy}, current_profile_version=5
    )
    assert report.outcome == "BLOCKED"
    assert {"CV_CONTACT_EMAIL", "CV_CONTACT_PHONE"} <= _codes(report, "BLOCK")


def test_package_pdf_identical_to_other_package_is_flagged(tmp_path):
    target = _package(tmp_path, "sample_tester")
    other = _package(tmp_path, "other_support")
    shutil.copy(other / "Max_Muster_Lebenslauf.pdf", target / "Max_Muster_Lebenslauf.pdf")
    report = validate_package(
        target, _expect(letter_required=False), sibling_packages=[target, other]
    )
    assert "CV_SAME_FILE_AS_OTHER_PACKAGE" in _codes(report, "WARN")


def test_pdf_swapped_after_binding_is_blocked(tmp_path):
    """Codex scenario: replace the PDF by another valid one with the same
    page count, keep source and manifest, attach an exact copy."""
    package = _package(tmp_path, "example_tester")
    write_manifest(package, 5)
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(_pdf(2, ["someone else"]))
    copy = tmp_path / "copy.pdf"
    copy.write_bytes((package / "Max_Muster_Lebenslauf.pdf").read_bytes())
    report = validate_package(
        package, _expect(letter_required=False), attached={"cv": copy}, current_profile_version=5
    )
    assert report.outcome == "BLOCKED"
    assert "CV_PDF_CHANGED" in _codes(report, "BLOCK")


def test_source_edited_after_binding_is_blocked(tmp_path):
    package = _package(tmp_path, "example_tester")
    write_manifest(package, 5)
    source = package / "_quelle_html" / "_cv.html"
    source.write_text(source.read_text(encoding="utf-8") + "<p>edit</p>", encoding="utf-8")
    report = validate_package(package, _expect(letter_required=False))
    assert "CV_SOURCE_CHANGED" in _codes(report, "BLOCK")


def test_pdf_without_xref_or_root_is_unreadable():
    assert pdf_page_count(b"%PDF-1.7\n/Type /Page\n%%EOF") is None


def test_letter_only_upload_check_is_not_ready(tmp_path):
    """Codex round 3: comparing only the letter leaves the CV the portal
    pre-filled unchecked."""
    package = _package(tmp_path, "example_tester")
    copy = tmp_path / "letter.pdf"
    copy.write_bytes((package / "Max_Muster_Anschreiben.pdf").read_bytes())
    report = validate_package(package, _expect(), attached={"letter": copy})
    assert report.outcome == "NEEDS_OPERATOR"
    assert "UPLOAD_NOT_CHECKED" in _codes(report, "WARN")


def test_long_pdf_is_not_parsed(tmp_path, monkeypatch):
    import app.services.pre_submit_validation as validation

    package = _package(tmp_path, "example_tester")
    (package / "Max_Muster_Lebenslauf.pdf").write_bytes(_pdf(40))
    parsed = []
    original = validation.pdf_text
    monkeypatch.setattr(validation, "pdf_text", lambda p: parsed.append(p.name) or original(p))
    report = validate_package(package, _expect())
    assert "CV_TOO_MANY_PAGES" in _codes(report, "WARN")
    assert "Max_Muster_Lebenslauf.pdf" not in parsed


def test_page_objects_the_byte_scan_cannot_see_are_counted_by_the_parser():
    """Codex round 4: page objects in a form the raw byte scan misses (here a
    hex-escaped name, standing in for compressed object streams) are still
    counted instead of being reported unreadable."""
    data = _pdf(2).replace(b"/Type /Page /Parent", b"/Type /P#61ge /Parent")
    assert pdf_page_count(data) == 2


def _object_stream_pdf(pages: int) -> bytes:
    """A real PDF 1.5 file whose catalog, page tree and page objects live in
    a Flate-compressed object stream with an xref stream (no `/Type /Page`
    bytes anywhere in the file)."""
    import zlib

    content = b"BT /F1 9 Tf 40 800 Td (" + FILLER.encode("latin-1") + b") Tj ET"
    page_ids = [3 + 2 * i for i in range(pages)]
    packed = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % p for p in page_ids)
        + b"] /Count %d >>" % pages,
    }
    font = 3 + 2 * pages
    for p in page_ids:
        packed[p] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font, p + 1)
        )
    packed[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    order = sorted(packed)
    body, header = b"", []
    for key in order:
        header.append(b"%d %d" % (key, len(body)))
        body += packed[key] + b" "
    head = b" ".join(header) + b" "
    objstm_id = font + 1
    xref_id = objstm_id + 1
    stream = zlib.compress(head + body)
    out, offsets = b"%PDF-1.5\n", {}
    for p in page_ids:
        offsets[p + 1] = len(out)
        out += (
            b"%d 0 obj\n<< /Length %d >>\nstream\n" % (p + 1, len(content))
            + content
            + b"\nendstream\nendobj\n"
        )
    offsets[objstm_id] = len(out)
    out += (
        b"%d 0 obj\n<< /Type /ObjStm /N %d /First %d /Filter /FlateDecode /Length %d >>\n"
        % (objstm_id, len(order), len(head), len(stream))
        + b"stream\n"
        + stream
        + b"\nendstream\nendobj\n"
    )
    offsets[xref_id] = len(out)
    rows = [b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")]
    for number in range(1, xref_id + 1):
        if number in packed:
            rows.append(
                b"\x02" + objstm_id.to_bytes(4, "big") + order.index(number).to_bytes(2, "big")
            )
        else:
            rows.append(b"\x01" + offsets[number].to_bytes(4, "big") + (0).to_bytes(2, "big"))
    xref = b"".join(rows)
    out += (
        b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root 1 0 R /Length %d >>\n"
        % (xref_id, xref_id + 1, len(xref))
        + b"stream\n"
        + xref
        + b"\nendstream\nendobj\n"
    )
    out += b"startxref\n%d\n%%%%EOF\n" % offsets[xref_id]
    return out


def test_pages_in_a_compressed_object_stream_are_counted():
    """Codex round 5 (P3): the real PDF 1.5 case, not an escaped-name proxy."""
    data = _object_stream_pdf(2)
    assert b"/Type /Page" not in data
    assert pdf_page_count(data) == 2


def test_mixed_escaped_page_names_are_never_undercounted(tmp_path):
    """Codex round 5: one plain `/Page` plus one `/P#61ge` is two pages, so a
    two-page letter cannot pass a one-page limit."""
    data = _pdf(2)
    first = data.index(b"/Type /Page /Parent")
    second = data.index(b"/Type /Page /Parent", first + 1)
    mixed = data[:second] + b"/Type /P#61ge /Parent" + data[second + len(b"/Type /Page /Parent") :]
    assert pdf_page_count(mixed) == 2
    package = _package(tmp_path, "example_tester")
    (package / "Max_Muster_Anschreiben.pdf").write_bytes(mixed)
    report = validate_package(package, _expect())
    assert "LETTER_TOO_MANY_PAGES" in _codes(report, "WARN")


def test_document_missing_from_the_manifest_is_never_ready(tmp_path):
    """Codex round 5: binding without the CV source must not yield READY."""
    package = _package(tmp_path, "example_tester")
    (package / "_quelle_html" / "_cv.html").unlink()
    upload = tmp_path / "upload"
    upload.mkdir()
    copy = shutil.copy(package / "Max_Muster_Lebenslauf.pdf", upload / "cv.pdf")
    manifest = write_manifest(package, 5)
    assert "cv" not in manifest["documents"]
    report = validate_package(
        package, _expect(letter_required=False), attached={"cv": copy}, current_profile_version=5
    )
    assert "CV_NOT_BOUND" in _codes(report, "WARN")
    assert report.outcome == "NEEDS_OPERATOR"


def test_presubmit_check_is_a_mandatory_step_for_the_browser_agent():
    """The check must be wired into the agent instructions and the submission
    flow, not only described as a standalone tool."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    agent = (root / "CLAUDE.md").read_text(encoding="utf-8")
    gate = agent.split("## Browser submission gate", 1)[1].split("\n## ", 1)[0]
    assert "scripts/presubmit_check.py" in gate and "mandatory" in gate
    assert "BLOCKED" in gate and "do not submit" in gate
    operations = (root / "docs" / "DAILY_OPERATIONS.md").read_text(encoding="utf-8")
    flow = operations.split("## 7. Preparing a Bewerbung", 1)[1].split("### 7a.", 1)[0]
    assert "presubmit_check.py" in flow and "mandatory" in flow
    record = operations.split("## 8. Record a submission", 1)[1].split("\n## ", 1)[0]
    assert "pre-submit outcome" in record


def test_without_a_confirmed_level_a_c1_claim_is_an_overclaim(tmp_path):
    """Codex round 12: no confirmed German level means a C1 claim is never
    silently accepted."""
    package = _package(
        tmp_path,
        "example_tester",
        cv=f"<html><body><p>{EMAIL} · {PHONE}</p><p>Deutsch C1</p></body></html>",
    )
    report = validate_package(package, _expect(german_level=None, letter_required=False))
    assert "CV_GERMAN_OVERCLAIM" in _codes(report, "BLOCK")


def test_cli_ignores_an_unconfirmed_imported_german_level(tmp_path, capsys):
    """Codex round 12: the CLI uses the same trust rule as the fit
    assessment; an IMPORTED C1 does not confirm a "Deutsch C1" claim."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.db.models import CandidateLanguageRecord
    from tests.test_stage17_job_fit import migrated as _migrated

    db_path = _migrated.__wrapped__(tmp_path)  # the Stage 17 fixture's factory
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with sessionmaker(engine)() as session:
        german = session.scalars(select(CandidateLanguageRecord)).one()
        german.level, german.source = "C1", "IMPORTED"
        session.commit()
    engine.dispose()
    package = _package(
        tmp_path,
        "example_tester",
        cv="<html><body><p>Max Muster</p><p>Deutsch C1</p></body></html>",
    )
    args = ["--database", str(db_path), "--package", str(package)]
    args += ["--company", "Example GmbH", "--role", "Softwaretester (m/w/d)", "--no-letter"]
    assert presubmit_check.run(args) == 4
    assert "CV_GERMAN_OVERCLAIM" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("claim", "confirmed", "flagged"),
    [
        # Codex round 13: a B2 claim is checked like C1.
        ("Deutsch B2", None, ["B2"]),
        ("Deutsch B2", "B1", ["B2"]),
        ("B2-Niveau in Deutsch", "B1", ["B2"]),
        ("Deutsch B1", "B1", []),
        # A level stated for another language is not a German claim.
        ("Englisch B2, Deutsch B1", "B1", []),
        ("Englisch C1 · Deutsch B1", "B1", []),
    ],
)
def test_every_stated_german_level_is_checked(claim, confirmed, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, confirmed) == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 14: words between "Deutsch..." and the level.
        ("Deutschkenntnisse auf B2-Niveau", ["B2"]),
        ("Sprachniveau B2 (Deutsch)", ["B2"]),
        ("Deutschkenntnisse auf dem Niveau B1", []),
        # ... but never across another language or another level.
        ("Deutsch B1 und Englisch B2", []),
        ("Deutsch (B1), Englisch (C1)", []),
    ],
)
def test_claims_with_words_between_language_and_level(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 15: punctuation before the level, and another
        # language's row joined onto the German one by pdf_text.
        ("Deutschkenntnisse: Niveau B2", ["B2"]),
        ("Deutschkenntnisse Arabisch B2", []),
        ("Japanisch B2 Deutsch B1", []),
        # The real packages' truthful wording: a course is not a claim.
        ("SPRACHEN Deutsch B1 (B2-Kurs laufend) Englisch", []),
        ("Mein Deutsch ist auf B1-Niveau zertifiziert, ein B2-Kurs läuft", []),
        ("Deutsch B1, Ziel: B2", []),
        ("Deutsch B2 (zertifiziert)", ["B2"]),
    ],
)
def test_german_levels_are_read_per_mention(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 16.
        ("Arabisch: Niveau B2 Deutsch B1", []),
        ("Deutsch: verhandlungssicher", ["C1"]),
        ("Deutsch – Muttersprache", ["NATIVE"]),
        ("Deutsch B1, Englisch fließend", []),
    ],
)
def test_row_owner_and_descriptors_after_the_mention(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 17: a descriptor before "Deutsch" has an owner too.
        ("Arabisch: Muttersprache Deutsch: B1", []),
        ("Englisch fließend, Deutsch B1", []),
        ("Muttersprache Deutsch", ["NATIVE"]),
        ("verhandlungssicheres Deutsch", ["C1"]),
    ],
)
def test_descriptor_before_the_mention_belongs_to_its_row(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 18: ordinary language-list wording joined by pdf_text.
        ("Deutsch B1, Muttersprache: Arabisch", []),
        ("Arabisch: in der Familie meine Muttersprache Deutsch: B1", []),
        ("Englisch B2, Muttersprache Deutsch", ["NATIVE"]),
    ],
)
def test_descriptor_owner_is_the_language_of_its_comma_part(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged


@pytest.mark.parametrize(
    ("claim", "flagged"),
    [
        # Codex round 19: two CV rows joined by pdf_text; the next "Label:"
        # starts a new row.
        ("Deutsch: B1 Muttersprache: Arabisch", []),
        ("Deutsch: B2 Englisch: C1", ["B2"]),
        ("Deutschkenntnisse: Niveau B2", ["B2"]),
    ],
)
def test_next_row_label_ends_the_german_statement(claim, flagged):
    from app.services.pre_submit_validation import _overclaimed_german

    assert _overclaimed_german(claim, "B1") == flagged
