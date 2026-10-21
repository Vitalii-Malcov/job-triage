"""Package isolation: the package for company A never carries company B's
documents, even when both are exported into the same directory.

`export_package_documents` plans only the files it has just written from
its own inputs (no directory listing), and never overwrites a file.
"""

import zipfile

import pytest

from app.models.review_package import (
    ReviewedBewerbungContent,
    ReviewedBewerbungParagraph,
    ReviewTextField,
)
from app.services import document_export as export
from tests.test_stage14d_live_pilot_readiness import _cv, _identity


def _letter(company: str) -> ReviewedBewerbungContent:
    return ReviewedBewerbungContent(
        subject=ReviewTextField(value=f"Bewerbung bei {company}"),
        salutation=ReviewTextField(value="Sehr geehrte Damen und Herren,"),
        opening=ReviewTextField(value=f"mit Interesse habe ich die Anzeige von {company} gelesen."),
        body_paragraphs=[ReviewedBewerbungParagraph(text="Ich bringe Kenntnisse in Python mit.")],
        closing=ReviewTextField(value="Mit freundlichen Grüßen"),
        signature_name=ReviewTextField(value="Erika Mustermann"),
    )


def _export(directory, company, reference):
    attachments, warnings = export.export_package_documents(
        cv=_cv(),
        reviewed_bewerbung=_letter(company),
        identity=_identity(),
        company=company,
        reference=reference,
        directory=directory,
    )
    plan = export.build_attachment_plan(
        job_id=1,
        vacancy_reference=reference,
        channel="EMAIL",
        attachments=attachments,
        document_warnings=warnings,
        review_status="APPROVED",
    )
    return plan


def _docx_text(path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def test_company_a_package_never_contains_company_b_documents(tmp_path):
    plan_b = _export(tmp_path, "Beta GmbH", "REF-B")
    plan_a = _export(tmp_path, "Alpha AG", "REF-A")
    a_paths = {item.document.path for item in plan_a.attachments}
    b_paths = {item.document.path for item in plan_b.attachments}
    assert len(a_paths) == len(b_paths) == 2 and not a_paths & b_paths
    for item in plan_a.attachments:
        assert "Alpha-AG_REF-A" in item.document.filename
        assert "Beta" not in item.document.filename
    letter_a = next(i for i in plan_a.attachments if i.role == "COVER_LETTER")
    text = _docx_text(letter_a.document.path)
    assert "Alpha AG" in text and "Beta GmbH" not in text
    # Directory contents do not leak into a plan: B's files are still there.
    assert len(list(tmp_path.glob("*.docx"))) == 4
    rendered = export.render_attachment_plan(plan_a)
    assert "Beta" not in rendered and "REF-B" not in rendered


def test_re_export_for_the_same_package_refuses_to_overwrite(tmp_path):
    _export(tmp_path, "Alpha AG", "REF-A")
    with pytest.raises(export.DocumentExistsError):
        _export(tmp_path, "Alpha AG", "REF-A")
