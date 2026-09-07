"""Unit tests for app.agents.follow_up_generator's deterministic DE/EN
follow-up content generation — mirrors tests/test_response_draft_generator.py's
style (no LLM, no invented facts, deterministic across repeated calls).
"""

from app.agents.follow_up_generator import generate_follow_up_content


class TestDeterminism:
    def test_same_inputs_produce_identical_output(self):
        first = generate_follow_up_content(
            language="en",
            candidate_name="Jane Doe",
            job_title="Backend Engineer",
            job_company="Globex",
        )
        second = generate_follow_up_content(
            language="en",
            candidate_name="Jane Doe",
            job_title="Backend Engineer",
            job_company="Globex",
        )
        assert first == second


class TestEnglishContent:
    def test_full_facts_produce_no_missing_fields(self):
        content = generate_follow_up_content(
            language="en",
            candidate_name="Jane Doe",
            job_title="Backend Engineer",
            job_company="Globex",
        )
        assert content.missing_fields == ()
        assert "Backend Engineer (Globex)" in content.subject
        assert "Backend Engineer (Globex)" in content.body
        assert content.body.endswith("Jane Doe")
        assert "Dear Hiring Team" in content.body

    def test_missing_job_produces_placeholder_and_missing_field(self):
        content = generate_follow_up_content(
            language="en", candidate_name="Jane Doe", job_title=None, job_company=None
        )
        assert "[position/company unknown" in content.subject
        assert any("matched job/company" in field for field in content.missing_fields)

    def test_missing_candidate_name_produces_placeholder_and_missing_field(self):
        content = generate_follow_up_content(
            language="en", candidate_name=None, job_title="Backend Engineer", job_company="Globex"
        )
        assert content.body.endswith("[Your Name]")
        assert any("candidate name" in field for field in content.missing_fields)


class TestGermanContent:
    def test_full_facts_render_german_template(self):
        content = generate_follow_up_content(
            language="de",
            candidate_name="Jane Doe",
            job_title="Backend Engineer",
            job_company="Globex",
        )
        assert content.missing_fields == ()
        assert "Sehr geehrte Damen und Herren" in content.body
        assert "Mit freundlichen Grüßen" in content.body
        assert "Backend Engineer (Globex)" in content.subject


class TestNeverInventsFacts:
    def test_generated_text_never_contains_untrusted_placeholder_content(self):
        """This module accepts no email subject/body/from_address input at
        all — there is nothing for an attacker-controlled string to reach.
        Confirmed structurally: the function signature has no such
        parameter (see its own module docstring)."""
        import inspect

        from app.agents import follow_up_generator

        sig = inspect.signature(follow_up_generator.generate_follow_up_content)
        assert "subject" not in sig.parameters
        assert "body_plain" not in sig.parameters
        assert "from_address" not in sig.parameters

    def test_job_only_title_without_company_uses_title_alone(self):
        content = generate_follow_up_content(
            language="en", candidate_name="Jane Doe", job_title="Backend Engineer", job_company=None
        )
        assert content.missing_fields == ()
        assert "Backend Engineer" in content.subject
        assert "(None)" not in content.subject
