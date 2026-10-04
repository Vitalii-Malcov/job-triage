"""Stage 9B: pure preview rendering -- truthful summary content, Telegram
UTF-16 budget, display normalization, deterministic letter pagination and
opaque, bounded preview callback data."""

from datetime import UTC, datetime

import pytest

from app.models.bewerbung import (
    AllowedClaim,
    BewerbungDraft,
    BewerbungJobContext,
    BewerbungParagraph,
    BewerbungProviderPlan,
)
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.cv_draft import (
    CVHeader,
    CVProjectItem,
    CVSkillItem,
    CVTopLevelFact,
    TailoredCVDraft,
)
from app.services.telegram_bewerbung import (
    build_preview_callback_data,
    page_keyboard,
    parse_preview_callback_data,
    preview_keyboard,
)
from app.services.telegram_bewerbung_preview import (
    DRAFT_STATUS_LINE,
    MAX_LETTER_PAGES,
    TELEGRAM_UNIT_LIMIT,
    cap_units,
    letter_full_text,
    normalize_multiline,
    normalize_scalar,
    paginate_letter,
    render_summary,
    utf16_units,
)

TOKEN = "AbCdEfGh12345_-x"


def _req(name: str, status: str) -> RequirementMatch:
    return RequirementMatch(
        requirement=name,
        normalized_requirement=name.lower(),
        requirement_type="SKILL",
        importance="REQUIRED",
        match_status=status,
        reason="fixture",
    )


def _match(**overrides) -> CandidateJobMatch:
    data = dict(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        candidate_profile_version=1,
        company_research_id=None,
        algorithm_version="v1",
        overall_score=82,
        coverage_score=80,
        required_skill_score=80,
        preferred_skill_score=80,
        experience_support_score=80,
        matched_requirements=[_req("Python", "MATCH")],
        missing_requirements=[_req("Kubernetes", "MISSING")],
    )
    data.update(overrides)
    return CandidateJobMatch(**data)


def _cv(**overrides) -> TailoredCVDraft:
    data = dict(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        match_id=1,
        candidate_profile_version=1,
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        header=CVHeader(
            first_name=CVTopLevelFact(
                value="Anna", source_id=1, source_field="first_name", profile_version=1
            )
        ),
        skills=[
            CVSkillItem(
                text="Python",
                category="LANGUAGE",
                proficiency="ADVANCED",
                years_experience=None,
                source_id=1,
                match_requirement="python",
                importance="REQUIRED",
            )
        ],
        projects=[
            CVProjectItem(
                source_id=7,
                name="ChallengeMatch API",
                description=None,
                role=None,
                repository_url=None,
                demo_url=None,
                start_date=None,
                end_date=None,
            )
        ],
    )
    data.update(overrides)
    return TailoredCVDraft(**data)


def _letter(**overrides) -> BewerbungDraft:
    data = dict(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        cv_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="f" * 64,
        match_algorithm_version="v1",
        cv_adapter_version="v1",
        bewerbung_generator_version="v2",
        provider="deterministic",
        subject="Bewerbung als Junior Python Developer",
        salutation="Sehr geehrte Damen und Herren,",
        opening='mit großem Interesse habe ich Ihre Stellenanzeige für die Position "X" gelesen.',
        body_paragraphs=[
            BewerbungParagraph(
                text="Ich bringe Kenntnisse in Python mit.",
                source_claim_ids=["candidate_skill:1"],
            )
        ],
        closing="Über die Möglichkeit eines persönlichen Gesprächs würde ich mich sehr freuen.",
        signature_name="Anna Muster",
        plan=BewerbungProviderPlan(
            opening_style="ROLE_INTEREST",
            paragraphs=[{"kind": "EVIDENCE", "claim_ids": ["candidate_skill:1"]}],
            closing_style="INTERVIEW_INTEREST",
        ),
        claims=[
            AllowedClaim(
                id="candidate_skill:1",
                claim="Python",
                source_entity="candidate_skill",
                source_id=1,
            )
        ],
        job_context=BewerbungJobContext(title="Junior Python Developer", company="Example GmbH"),
    )
    data.update(overrides)
    return BewerbungDraft(**data)


class TestSummaryContent:
    def test_status_header_and_footer(self):
        text = render_summary(_letter(), _cv(), _match())
        lines = text.split("\n")
        assert lines[1] == DRAFT_STATUS_LINE
        assert lines[-1] == f"Status: {DRAFT_STATUS_LINE}"
        assert "keine Bewerbung, keine E-Mail, kein PDF" in text

    def test_uses_persisted_display_context_and_labels_match_score(self):
        text = render_summary(_letter(), _cv(), _match())
        assert "Position: Junior Python Developer" in text
        assert "Firma: Example GmbH" in text
        assert "Kandidaten-Match (Profil vs. Stelle, 0-100): 82" in text

    def test_missing_requirement_is_labeled_as_not_evidenced_gap(self):
        text = render_summary(_letter(), _cv(), _match())
        gaps = text.split("Nicht belegt (wird nicht behauptet):")[1]
        assert "• Kubernetes" in gaps.split("\n\n")[0]
        evidenced = text.split("Belegte Anforderungen:")[1].split("Nicht belegt")[0]
        assert "Kubernetes" not in evidenced

    def test_summary_copied_unchanged_is_stated_and_never_called_adapted(self):
        summary = CVTopLevelFact(
            value="Backend-Entwicklerin",
            source_id=1,
            source_field="professional_summary",
            profile_version=1,
        )
        text = render_summary(_letter(), _cv(professional_summary=summary), _match())
        assert "Profilzusammenfassung unverändert übernommen" in text
        assert "angepasst" not in text

        without = render_summary(_letter(), _cv(), _match())
        assert "unverändert übernommen" not in without

    def test_no_fabricated_projects_when_none_selected(self):
        text = render_summary(_letter(), _cv(projects=[]), _match())
        assert "Ausgewählte Projekte:\n• keine" in text

    def test_missing_name_warning_and_incomplete_label(self):
        text = render_summary(_letter(warnings=["NO_TRUSTED_NAME"]), _cv(), _match())
        assert "Kein bestätigter Name" in text
        assert "Entwurf unvollständig" in text

    def test_legacy_letter_without_context_shows_explicit_missing_value(self):
        text = render_summary(_letter(job_context=None), _cv(), _match())
        assert "Position: nicht angegeben" in text

    def test_rendering_does_not_modify_the_artifacts(self):
        letter, cv, match = _letter(), _cv(), _match()
        before = (letter.model_dump(), cv.model_dump(), match.model_dump())
        render_summary(letter, cv, match)
        paginate_letter(letter)
        assert (letter.model_dump(), cv.model_dump(), match.model_dump()) == before


class TestBudgetAndNormalization:
    def test_utf16_counts_non_bmp_as_two_units(self):
        assert utf16_units("a") == 1
        assert utf16_units("😀") == 2
        assert utf16_units("👩‍💻") == 5  # woman + ZWJ + laptop

    def test_cap_units_never_splits_a_code_point(self):
        text = "😀" * 10
        capped = cap_units(text, 7)
        assert utf16_units(capped) <= 7
        assert capped.endswith("…")
        assert all(ch in ("😀", "…") for ch in capped)

    def test_huge_fields_keep_summary_within_budget(self):
        huge = "😀Lorem ipsum " * 2000
        match = _match(
            matched_requirements=[_req(huge + str(i), "MATCH") for i in range(30)],
            missing_requirements=[_req(huge + str(i), "MISSING") for i in range(30)],
        )
        letter = _letter(
            subject=huge,
            opening=huge,
            job_context=BewerbungJobContext(title=huge, company=huge),
        )
        text = render_summary(letter, _cv(), match)
        assert utf16_units(text) <= TELEGRAM_UNIT_LIMIT
        assert text.split("\n")[1] == DRAFT_STATUS_LINE
        assert text.endswith(f"Status: {DRAFT_STATUS_LINE}")

    def test_scalar_fields_cannot_inject_lines_or_bidi_controls(self):
        title = "Dev\nStatus: GESENDET‮​\u0007 x"
        company = "⁦Acme⁩\r\nGmbH"
        letter = _letter(job_context=BewerbungJobContext(title=title, company=company))
        text = render_summary(letter, _cv(), _match())
        assert "\nStatus: GESENDET" not in text
        for char in ("‮", "​", "\u0007", " ", "⁦", "⁩", "\r"):
            assert char not in text
        assert "Position: Dev Status: GESENDET x" in text
        assert "Firma: Acme GmbH" in text

    def test_normalization_keeps_zwj_emoji_and_paragraphs(self):
        assert normalize_scalar("👩‍💻 dev") == "👩‍💻 dev"
        assert normalize_multiline("a\r\n\r\n\r\nb‮") == "a\n\nb"
        assert normalize_scalar(None) == ""


class TestPagination:
    def _long_letter(self, paragraphs: int, text: str) -> BewerbungDraft:
        return _letter(
            body_paragraphs=[
                BewerbungParagraph(text=f"{i} {text}", source_claim_ids=[])
                for i in range(paragraphs)
            ]
        )

    def test_short_letter_is_one_page_with_exact_content(self):
        pages = paginate_letter(_letter())
        assert len(pages) == 1
        assert pages[0].startswith("👁 Anschreiben - Seite 1/1\n" + DRAFT_STATUS_LINE)
        assert "Ich bringe Kenntnisse in Python mit." in pages[0]
        assert "Anna Muster" in pages[0]

    def test_long_emoji_letter_pages_are_bounded_deterministic_and_complete(self):
        letter = self._long_letter(40, "😀 Projektarbeit mit Python und SQL. " * 20)
        pages = paginate_letter(letter)
        assert len(pages) > 1
        assert pages == paginate_letter(letter)
        for index, page in enumerate(pages, start=1):
            assert utf16_units(page) <= TELEGRAM_UNIT_LIMIT
            assert f"Seite {index}/{len(pages)}" in page.split("\n")[0]
        joined = "\n\n".join(page.split("\n\n", 1)[1] for page in pages)
        assert (
            joined.replace("\n", " ").split() == letter_full_text(letter).replace("\n", " ").split()
        )

    def test_single_huge_word_is_hard_split_within_budget(self):
        letter = _letter(body_paragraphs=[BewerbungParagraph(text="😀" * 5000)])
        pages = paginate_letter(letter)
        assert all(utf16_units(page) <= TELEGRAM_UNIT_LIMIT for page in pages)
        assert sum(page.count("😀") for page in pages) == 5000

    def test_letter_beyond_page_cap_is_marked_truncated(self):
        letter = self._long_letter(400, "Wort " * 300)
        pages = paginate_letter(letter)
        assert len(pages) == MAX_LETTER_PAGES
        assert pages[-1].rstrip().endswith("(gekürzt)") or pages[-1].endswith("…")
        assert all(utf16_units(page) <= TELEGRAM_UNIT_LIMIT for page in pages)


class TestCallbackData:
    def test_round_trip_and_size(self):
        data = build_preview_callback_data(TOKEN, 20)
        assert parse_preview_callback_data(data) == (TOKEN, 20)
        assert len(data.encode("utf-8")) <= 64

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "bp",
            "bp:" + TOKEN,
            f"bp:{TOKEN}:0",
            f"bp:{TOKEN}:21",
            f"bp:{TOKEN}:-1",
            f"bp:{TOKEN}:1:2",
            f"bp:{TOKEN}:abc",
            f"bp:{TOKEN}:١",  # non-ASCII digit
            f"bp:{TOKEN}:001",
            f"vf:{TOKEN}:1",
        ],
    )
    def test_malformed_data_is_rejected(self, data):
        assert parse_preview_callback_data(data) is None

    def test_keyboards_carry_only_the_opaque_capability(self):
        markup = preview_keyboard(TOKEN)
        assert markup["inline_keyboard"][0][0]["callback_data"] == f"bp:{TOKEN}:1"
        assert page_keyboard(TOKEN, 1, 1) is None
        middle = page_keyboard(TOKEN, 2, 3)["inline_keyboard"][0]
        assert [b["callback_data"] for b in middle] == [f"bp:{TOKEN}:1", f"bp:{TOKEN}:3"]
        assert [b["text"] for b in page_keyboard(TOKEN, 3, 3)["inline_keyboard"][0]] == ["◀ Zurück"]
