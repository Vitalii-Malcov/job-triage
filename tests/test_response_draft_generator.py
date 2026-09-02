"""Pure-function tests for app.agents.response_draft_generator (Stage 7C).

Covers: every supported classification x language produces a draft;
unsupported classifications return None; missing candidate/job facts are
represented as placeholders + missing_fields, never guessed; the
generator's own signature never accepts email-derived text at all (the
structural prompt-injection defense — see module docstring).
"""

import inspect

from app.agents.response_draft_generator import (
    SUPPORTED_RESPONSE_CLASSIFICATIONS,
    detect_language,
    generate_response_draft,
)

_ALL_CLASSIFICATIONS = (
    "APPLICATION_RECEIVED",
    "REQUEST_FOR_INFORMATION",
    "INTERVIEW_INVITATION",
    "INTERVIEW_RESCHEDULE",
    "REJECTION",
    "OFFER",
    "WITHDRAWAL_OR_POSITION_CLOSED",
    "GENERAL_RECRUITER_MESSAGE",
    "AUTOMATED_NOTIFICATION",
    "OTHER",
    "UNKNOWN",
)

_UNSUPPORTED_CLASSIFICATIONS = tuple(
    c for c in _ALL_CLASSIFICATIONS if c not in SUPPORTED_RESPONSE_CLASSIFICATIONS
)


class TestSupportedClassificationSet:
    def test_supported_set_matches_spec(self):
        assert SUPPORTED_RESPONSE_CLASSIFICATIONS == {
            "REQUEST_FOR_INFORMATION",
            "INTERVIEW_INVITATION",
            "INTERVIEW_RESCHEDULE",
            "OFFER",
            "GENERAL_RECRUITER_MESSAGE",
        }

    def test_every_all_classification_is_accounted_for(self):
        assert set(_ALL_CLASSIFICATIONS) == SUPPORTED_RESPONSE_CLASSIFICATIONS | set(
            _UNSUPPORTED_CLASSIFICATIONS
        )


class TestUnsupportedClassificationsReturnNone:
    def test_unsupported_classifications_all_return_none(self):
        for classification in _UNSUPPORTED_CLASSIFICATIONS:
            result = generate_response_draft(
                classification=classification,
                language="en",
                candidate_name="Jane Doe",
                job_title="Python Developer",
                job_company="Acme GmbH",
            )
            assert result is None, f"{classification} unexpectedly produced a draft"


class TestSupportedClassificationsProduceDrafts:
    def test_every_supported_classification_x_language_produces_content(self):
        for classification in sorted(SUPPORTED_RESPONSE_CLASSIFICATIONS):
            for language in ("de", "en"):
                result = generate_response_draft(
                    classification=classification,
                    language=language,
                    candidate_name="Jane Doe",
                    job_title="Python Developer",
                    job_company="Acme GmbH",
                )
                assert result is not None
                assert result.subject
                assert result.body
                assert result.language == language
                assert "Python Developer" in result.subject or "Python Developer" in result.body
                assert "Acme GmbH" in result.subject or "Acme GmbH" in result.body
                assert "Jane Doe" in result.body
                # Every supported classification has at least one inherent
                # "the generator never read email content" limitation (see
                # module docstring) — but with full candidate/job facts
                # supplied, neither the name nor the job placeholder
                # reasons should appear.
                assert not any("candidate name" in field for field in result.missing_fields)
                assert not any("matched job" in field for field in result.missing_fields)

    def test_german_template_uses_german_salutation(self):
        result = generate_response_draft(
            classification="INTERVIEW_INVITATION",
            language="de",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert result.body.startswith("Sehr geehrte Damen und Herren,")

    def test_english_template_uses_english_salutation(self):
        result = generate_response_draft(
            classification="INTERVIEW_INVITATION",
            language="en",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert result.body.startswith("Dear Hiring Team,")


class TestMissingDataNeverInvented:
    def test_missing_candidate_name_yields_placeholder_and_missing_field(self):
        result = generate_response_draft(
            classification="INTERVIEW_INVITATION",
            language="en",
            candidate_name=None,
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert "[Your Name]" in result.body
        assert any("candidate name" in field for field in result.missing_fields)
        # A guessed/fabricated name must never appear.
        assert "Jane" not in result.body

    def test_missing_job_yields_placeholder_and_missing_field(self):
        result = generate_response_draft(
            classification="GENERAL_RECRUITER_MESSAGE",
            language="en",
            candidate_name="Jane Doe",
            job_title=None,
            job_company=None,
        )
        assert "[position/company unknown" in result.subject or "[position/company unknown" in (
            result.body
        )
        assert any("matched job" in field for field in result.missing_fields)

    def test_job_title_without_company_is_used_alone_not_invented(self):
        result = generate_response_draft(
            classification="OFFER",
            language="en",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company=None,
        )
        assert "Python Developer" in result.subject
        # No fabricated company name and no missing-job placeholder either
        # (a title alone is real, partial, trusted data — not invented).
        assert "[position/company unknown" not in result.subject
        assert not any("matched job" in field for field in result.missing_fields)

    def test_request_for_information_always_flags_unknown_specifics(self):
        """The generator has no access to WHAT the recruiter asked for
        (it never reads email content) — this must always be flagged,
        never silently omitted or guessed.
        """
        result = generate_response_draft(
            classification="REQUEST_FOR_INFORMATION",
            language="en",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert any("specific information" in field for field in result.missing_fields)

    def test_offer_never_auto_decides_salary_or_start_date(self):
        result = generate_response_draft(
            classification="OFFER",
            language="en",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert any("must not be auto-decided" in field for field in result.missing_fields)
        assert "accept" not in result.body.lower()

    def test_interview_invitation_never_commits_to_a_time(self):
        result = generate_response_draft(
            classification="INTERVIEW_INVITATION",
            language="en",
            candidate_name="Jane Doe",
            job_title="Python Developer",
            job_company="Acme GmbH",
        )
        assert any("availability" in field for field in result.missing_fields)


class TestNoUntrustedInputSurface:
    def test_generator_signature_never_accepts_email_derived_text(self):
        """Structural guard (see module docstring): if a future change
        ever adds a subject/body/from_address parameter to this function,
        that reopens exactly the prompt-injection surface Stage 7C is
        designed to close by construction — fail loudly instead of
        silently accepting it.
        """
        params = set(inspect.signature(generate_response_draft).parameters)
        assert params == {
            "classification",
            "language",
            "candidate_name",
            "job_title",
            "job_company",
        }


class TestDetectLanguage:
    def test_german_text_detected_as_de(self):
        assert (
            detect_language(
                "Einladung zum Vorstellungsgespräch",
                "Wir freuen uns, Sie zu einem Gespräch einzuladen. Bitte teilen Sie uns einen "
                "Termin mit.",
            )
            == "de"
        )

    def test_english_text_detected_as_en(self):
        assert (
            detect_language(
                "Interview Invitation",
                "We would like to invite you for an interview. Please let us know your "
                "availability.",
            )
            == "en"
        )

    def test_tie_defaults_to_english(self):
        assert detect_language("", "") == "en"
