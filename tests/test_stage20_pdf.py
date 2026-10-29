"""Stage 20: PDF rendering of confirmed-fact document blocks."""

import io
import re
import zipfile

import pytest
from pypdf import PdfReader

import app.services.document_export as export
from app.agents.cv_document import Block, build_cv_document
from app.models.review_package import ReviewedBewerbungParagraph
from app.services import pdf_document as pdf
from app.services.pre_submit_validation import (
    MANIFEST,
    Expectation,
    validate_package,
    write_manifest,
)
from tests.test_stage14d_live_pilot_readiness import _cv, _fact, _identity, _reviewed_letter

GERMAN = "Müller & Söhne GmbH – „Übergänge“, Straße, Größe: 5 € (Klammer) \\ Backslash"


def _letter():
    """A realistic-length letter (presubmit needs >= 200 characters of text)."""
    paragraphs = [
        "Ihre Stelle als Entwickler bei der Beispiel AG passt zu meinen Kenntnissen in "
        "Python, FastAPI und SQL, die ich in eigenen Projekten eingesetzt habe.",
        "Gern stelle ich mich Ihnen in einem persönlichen Gespräch vor.",
    ]
    return _reviewed_letter().model_copy(
        update={"body_paragraphs": [ReviewedBewerbungParagraph(text=t) for t in paragraphs]}
    )


def _blocks(bullets: int = 3) -> list[Block]:
    return [
        Block("TITLE", "Lebenslauf"),
        Block("PARAGRAPH", "Erika Mustermann"),
        Block("HEADING", "Berufserfahrung"),
        Block("SUBHEADING", GERMAN),
        *[
            Block("BULLET", f"Punkt {i}: Entwicklung von REST-APIs mit Python und FastAPI. " * 3)
            for i in range(bullets)
        ],
    ]


def test_pdf_is_valid_deterministic_and_says_exactly_the_blocks():
    blocks = _blocks()
    rendered = pdf.render_pdf(blocks, title="Lebenslauf")
    assert rendered.content.startswith(b"%PDF-1.4")
    assert rendered.content.rstrip().endswith(b"%%EOF")
    assert pdf.render_pdf(blocks, title="Lebenslauf").content == rendered.content
    reader = PdfReader(io.BytesIO(rendered.content))
    assert len(reader.pages) == rendered.pages == 1
    text = pdf.extracted_text(rendered.content)
    assert "Müller & Söhne GmbH" in text and "„Übergänge“" in text and "5 €" in text
    assert "".join(text.split()) == "".join(pdf.expected_text(blocks).split())
    # no timestamps / IDs that would break hash binding
    for marker in (b"/CreationDate", b"/ModDate", b"/ID"):
        assert marker not in rendered.content


def test_long_documents_paginate_and_stay_inside_the_margins():
    blocks = _blocks(bullets=60) + [Block("PARAGRAPH", "Donaudampfschifffahrt" * 12)]
    rendered = pdf.render_pdf(blocks, title="Lebenslauf")
    assert rendered.pages >= 3
    right = pdf.PAGE_WIDTH - pdf.MARGIN + 0.01
    for page in pdf._layout(blocks):
        for op in page:
            size, x, y = map(
                float, re.match(rb"BT /F\d ([\d.]+) Tf ([\d.]+) ([\d.]+) Td", op).groups()
            )
            data = re.search(rb"\((.*)\) Tj", op, re.S).group(1)
            data = re.sub(rb"\\([()\\])", rb"\1", data)
            data = re.sub(rb"\\(\d{3})", lambda m: bytes([int(m.group(1), 8)]), data)
            assert pdf.MARGIN - 0.01 <= y <= pdf.PAGE_HEIGHT - pdf.MARGIN
            assert pdf.MARGIN <= x
            assert x + pdf._width(data, b"/F2" in op, size) <= right


def test_heading_is_not_left_alone_at_the_bottom_of_a_page():
    filler = [Block("PARAGRAPH", f"Zeile {i}") for i in range(52)]
    blocks = filler + [Block("HEADING", "Ausbildung"), Block("PARAGRAPH", "Studium")]
    pages = pdf._layout(blocks)
    heading_page = next(i for i, ops in enumerate(pages) if any(b"(Ausbildung)" in o for o in ops))
    body_page = next(i for i, ops in enumerate(pages) if any(b"(Studium)" in o for o in ops))
    assert heading_page == body_page


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Привет", ["П", "р", "и", "в", "е", "т"]),
        ("ok 😀", ["😀"]),
        ("bell \x07", ["\x07"]),
        ("ĳ ligature", ["ĳ"]),
    ],
)
def test_unrenderable_characters_are_refused_not_replaced(text, bad):
    with pytest.raises(pdf.UnrenderableTextError) as caught:
        pdf.render_pdf([Block("PARAGRAPH", text)], title="x")
    assert caught.value.characters == bad


def test_invisible_characters_are_normalised():
    rendered = pdf.render_pdf([Block("PARAGRAPH", "Python\u00a0Entwick\u00adler")], title="x")
    assert "PythonEntwickler" in "".join(pdf.extracted_text(rendered.content).split())


def test_text_mismatch_after_rendering_is_refused(monkeypatch):
    monkeypatch.setattr(pdf, "extracted_text", lambda _content: "something else")
    with pytest.raises(pdf.PdfVerificationError):
        pdf.render_pdf(_blocks(), title="Lebenslauf")


def test_package_export_as_pdf_passes_the_presubmit_check(tmp_path):
    identity = _identity()
    attachments, warnings = export.export_package_documents(
        cv=_cv(),
        reviewed_bewerbung=_letter(),
        identity=identity,
        company="Beispiel AG",
        reference="REF-1",
        directory=tmp_path,
        document_format="pdf",
    )
    assert warnings == ()
    names = sorted(a.document.filename for a in attachments)
    assert names == [
        "Anschreiben_Erika_Mustermann_Beispiel-AG_REF-1.pdf",
        "Lebenslauf_Erika_Mustermann_Beispiel-AG_REF-1.pdf",
    ]
    assert {a.document.media_type for a in attachments} == {"application/pdf"}
    cv_text = pdf.extracted_text((tmp_path / names[1]).read_bytes())
    expected = build_cv_document(_cv(), identity).blocks
    assert "".join(cv_text.split()) == "".join(pdf.expected_text(expected).split())

    def check(german_level):
        return validate_package(
            tmp_path,
            Expectation(
                company="Beispiel AG",
                role="Entwickler",
                contact_email=identity.email,
                contact_phone=identity.phone,
                german_level=german_level,
            ),
            attached={"cv": tmp_path / names[1], "letter": tmp_path / names[0]},
        )

    report = check("B1")  # the fixture CV states Deutsch B1
    assert report.outcome == "READY", report.findings
    assert {"CV_TEXT_OK", "LETTER_TEXT_OK"} <= {f.code for f in report.findings}
    # presubmit really reads the generated text: a lower confirmed level is caught
    assert "CV_GERMAN_OVERCLAIM" in {f.code for f in check("A2").findings}


def test_unrenderable_cv_writes_no_file_at_all(tmp_path):
    cv = _cv()
    cv = cv.model_copy(
        update={"professional_summary": _fact("Опыт работы", "professional_summary")}
    )
    with pytest.raises(pdf.UnrenderableTextError):
        export.export_package_documents(
            cv=cv,
            reviewed_bewerbung=_reviewed_letter(),
            identity=_identity(),
            company="Beispiel AG",
            reference="REF-1",
            directory=tmp_path,
            document_format="pdf",
        )
    assert list(tmp_path.iterdir()) == []


def test_docx_stays_the_default(tmp_path):
    attachments, _ = export.export_package_documents(
        cv=_cv(),
        reviewed_bewerbung=None,
        identity=_identity(),
        company="Beispiel AG",
        reference="REF-1",
        directory=tmp_path,
    )
    assert attachments[0].document.filename.endswith(".docx")
    assert attachments[0].document.media_type == export.DOCX_MEDIA_TYPE


def test_letter_sender_block_only_from_a_matching_confirmed_identity(tmp_path):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    kwargs = dict(cv=_cv(), reviewed_bewerbung=_letter(), company="Beispiel AG", reference="R")

    export.export_package_documents(identity=_identity(), directory=good, **kwargs)
    letter = next(good.glob("Anschreiben_*.docx")).read_bytes()
    assert b"E-Mail: erika.mustermann@example.org" in _docx_text(letter)

    _, warnings = export.export_package_documents(
        identity=_identity(full_name="Someone Else"), directory=bad, **kwargs
    )
    assert "CONTACT_NAME_MISMATCH" in warnings
    letter = next(bad.glob("Anschreiben_*.docx")).read_bytes()
    assert b"E-Mail:" not in _docx_text(letter)
    assert b"Someone Else" not in _docx_text(letter)


def _docx_text(content: bytes) -> bytes:
    return zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml")


def test_heading_chain_stays_with_its_content():
    """Codex review B (P3): 38 lines, then HEADING + SUBHEADING + text."""
    blocks = [Block("PARAGRAPH", f"Zeile {i}") for i in range(38)]
    blocks += [
        Block("HEADING", "Berufserfahrung"),
        Block("SUBHEADING", "Entwickler"),
        Block("PARAGRAPH", "Aufgaben"),
    ]

    def page_of(text: bytes) -> int:
        pages = pdf._layout(blocks)
        return next(i for i, ops in enumerate(pages) if any(b"(" + text + b")" in o for o in ops))

    assert page_of(b"Berufserfahrung") == page_of(b"Entwickler") == page_of(b"Aufgaben")


def test_very_long_words_split_in_linear_time_without_losing_characters():
    """Codex review B (P2): the old prefix search was cubic."""
    import time

    word = ("W" * 7 + "i") * 2500  # 20 000 characters, mixed widths
    started = time.perf_counter()
    chunks = pdf._split_long_word(word.encode(), False, 10.5, 400)
    assert time.perf_counter() - started < 0.5
    assert b"".join(chunks) == word.encode()
    assert all(pdf._width(c, False, 10.5) <= 400 for c in chunks)
    rendered = pdf.render_pdf([Block("PARAGRAPH", word)], title="x")
    assert "".join(pdf.extracted_text(rendered.content).split()) == word


# --- binding exported PDFs (presubmit_check.py --bind) ----------------------------

CV_PDF = "Lebenslauf_Erika_Mustermann_Beispiel-AG_REF-1.pdf"
LETTER_PDF = "Anschreiben_Erika_Mustermann_Beispiel-AG_REF-1.pdf"


def _export(directory, **overrides):
    values = dict(
        cv=_cv(),
        reviewed_bewerbung=_letter(),
        identity=_identity(),
        company="Beispiel AG",
        reference="REF-1",
        directory=directory,
        document_format="pdf",
    )
    values.update(overrides)
    return export.export_package_documents(**values)


def _check(package, profile_version=7):
    identity = _identity()
    return validate_package(
        package,
        Expectation(
            company="Beispiel AG",
            role="Entwickler",
            contact_email=identity.email,
            contact_phone=identity.phone,
            german_level="B1",
            profile_version=7,
        ),
        attached={"cv": package / CV_PDF, "letter": package / LETTER_PDF},
        current_profile_version=profile_version,
    )


def _codes(report, severity):
    return {f.code for f in report.findings if f.severity == severity}


def test_exported_pdfs_bind_to_their_sources_and_become_ready(tmp_path):
    _export(tmp_path)
    sources = tmp_path / "_quelle_html"
    assert sorted(p.name for p in sources.iterdir()) == ["_cv.html", "_letter.html"]
    assert "Erika Mustermann" in (sources / "_cv.html").read_text(encoding="utf-8")
    before = _check(tmp_path)
    assert before.outcome == "NEEDS_OPERATOR"  # nothing bound yet: still a human step
    manifest = write_manifest(tmp_path, 7)
    assert sorted(manifest["documents"]) == ["cv", "letter"] and "refused" not in manifest
    report = _check(tmp_path)
    assert report.outcome == "READY", report.findings
    # a newer candidate profile than the one the package was built from
    assert _check(tmp_path, profile_version=8).outcome != "READY"


def test_a_pdf_that_does_not_say_its_source_is_never_bound(tmp_path):
    _export(tmp_path)
    other = pdf.render_pdf(_blocks(), title="Lebenslauf").content  # valid PDF, other text
    (tmp_path / CV_PDF).write_bytes(other)
    manifest = write_manifest(tmp_path, 7)
    assert sorted(manifest["documents"]) == ["letter"]
    assert manifest["refused"] == {"cv": "PDF text does not match its exported source"}
    report = _check(tmp_path)
    assert "CV_NOT_BOUND" in _codes(report, "WARN")
    assert report.outcome != "READY"


def test_a_source_with_a_broken_text_marker_never_binds(tmp_path):
    _export(tmp_path)
    source = tmp_path / "_quelle_html" / "_cv.html"
    text = source.read_text(encoding="utf-8")
    source.write_text(re.sub(r'content="[0-9a-f]{64}"', 'content="x"', text), encoding="utf-8")
    assert "cv" in write_manifest(tmp_path, 7)["refused"]


def test_changes_after_binding_are_blocked(tmp_path):
    _export(tmp_path)
    write_manifest(tmp_path, 7)
    source = tmp_path / "_quelle_html" / "_letter.html"
    source.write_text(source.read_text(encoding="utf-8") + "<p>x</p>", encoding="utf-8")
    assert "LETTER_SOURCE_CHANGED" in _codes(_check(tmp_path), "BLOCK")
    cv = tmp_path / CV_PDF
    cv.write_bytes(cv.read_bytes() + b"\n")
    report = _check(tmp_path)
    assert "CV_PDF_CHANGED" in _codes(report, "BLOCK") and report.outcome == "BLOCKED"


def test_existing_sources_are_never_overwritten(tmp_path):
    sources = tmp_path / "_quelle_html"
    sources.mkdir()
    (sources / "_cv.html").write_text("operator's own source", encoding="utf-8")
    with pytest.raises(export.DocumentExistsError):
        _export(tmp_path)
    assert (sources / "_cv.html").read_text(encoding="utf-8") == "operator's own source"
    assert not list(tmp_path.glob("*.pdf")) and not (tmp_path / MANIFEST).exists()


def test_docx_export_writes_no_sources(tmp_path):
    _export(tmp_path, document_format="docx")
    assert not (tmp_path / "_quelle_html").exists()


def test_a_damaged_export_source_does_not_pass_as_stage19_html(tmp_path):
    # Codex review: invalid UTF-8 in an exported source must not skip the text check.
    _export(tmp_path)
    source = tmp_path / "_quelle_html" / "_cv.html"
    source.write_bytes(source.read_bytes() + bytes([0xFF]))
    (tmp_path / CV_PDF).write_bytes(pdf.render_pdf(_blocks(), title="Lebenslauf").content)
    assert "cv" in write_manifest(tmp_path, 7)["refused"]
    assert _check(tmp_path).outcome != "READY"
