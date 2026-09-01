"""Tests for app.services.email_matching (Stage 7B) — deterministic
job/application matching, precedence, ambiguity, and bounded evidence.
"""

from app.services.email_matching import (
    JobCandidate,
    ThreadPriorMatch,
    extract_company_domain,
    extract_sender_domain,
    match_email_to_job,
    normalize_company_name,
)


def _job(job_id, title, company, location="Berlin", url="https://example.com/jobs/1", status="NEW"):
    return JobCandidate(
        job_id=job_id, title=title, company=company, location=location, url=url, status=status
    )


def test_exact_application_reference_gives_high_confidence():
    jobs = [
        _job(
            1,
            "Python Developer",
            "Acme GmbH",
            url="https://acme.example.com/jobs/482173",
            status="APPLIED",
        ),
        _job(
            2,
            "Python Developer",
            "Other GmbH",
            url="https://other.example.com/careers/999",
            status="APPLIED",
        ),
    ]
    result = match_email_to_job(
        subject="Ihre Bewerbung Ref: 482173",
        body_plain="Vielen Dank für Ihre Bewerbung",
        from_address="hr@acme.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "APPLICATION"
    assert result.matched_job_id == 1
    assert result.confidence == "HIGH"
    assert any(item.kind == "JOB_REFERENCE" for item in result.evidence)


def test_exact_unique_company_and_job_combination():
    jobs = [_job(1, "Backend Engineer", "UniqueCo GmbH", status="NEW")]
    result = match_email_to_job(
        subject="Re: Backend Engineer bei UniqueCo",
        body_plain="UniqueCo GmbH bedankt sich für Ihre Bewerbung als Backend Engineer.",
        from_address="hr@somewhere-unrelated.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "JOB_ONLY"
    assert result.matched_job_id == 1


def test_same_title_across_multiple_companies_is_ambiguous():
    jobs = [
        _job(1, "Data Analyst Reporting", "FirstCo", url="https://firstco.example.com"),
        _job(2, "Data Analyst Reporting", "SecondCo", url="https://secondco.example.com"),
    ]
    result = match_email_to_job(
        subject="Re: Data Analyst Reporting",
        body_plain="Thanks for applying as Data Analyst Reporting.",
        from_address="hr@unrelated-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}
    assert result.matched_job_id is None


def test_same_company_multiple_jobs_is_ambiguous():
    jobs = [
        _job(1, "Data Analyst", "BigCo GmbH", url="https://bigco.example.com/a"),
        _job(2, "Marketing Lead", "BigCo GmbH", url="https://bigco.example.com/b"),
    ]
    result = match_email_to_job(
        subject="Bewerbung bei BigCo",
        body_plain="BigCo GmbH bedankt sich für Ihre Bewerbung.",
        from_address="hr@unrelated-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert {c.job_id for c in result.candidates} == {1, 2}


def test_generic_title_with_several_applications_is_ambiguous_not_guessed():
    """Spec's own worked example: five 'Python Developer' applications,
    a generic acknowledgement — no arbitrary winner.
    """
    jobs = [
        _job(
            i, "Python Developer", f"Company{i}", url=f"https://c{i}.example.com", status="APPLIED"
        )
        for i in range(1, 6)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        from_address="hr@ats.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert len(result.candidates) == 5
    assert result.confidence == "LOW"  # weak/generic evidence only


def test_unrelated_email_is_unmatched():
    jobs = [_job(1, "Python Developer", "Acme GmbH")]
    result = match_email_to_job(
        subject="Lunch tomorrow?",
        body_plain="Want to grab lunch tomorrow?",
        from_address="friend@gmail.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "UNMATCHED"
    assert result.matched_job_id is None
    assert result.score == 0


def test_existing_trusted_thread_association_wins_over_composite_scoring():
    jobs = [
        _job(1, "Python Developer", "Acme GmbH", status="APPLIED"),
        _job(3, "Totally Different Role", "SomeOtherCo", status="APPLIED"),
    ]
    result = match_email_to_job(
        subject="Re: Re: your application",
        body_plain="some unrelated follow-up text about scheduling",
        from_address="hr@random-domain.com",
        job_candidates=jobs,
        thread_prior_matches=[ThreadPriorMatch(job_id=3, match_type="APPLICATION")],
    )
    assert result.matched_job_id == 3
    assert result.match_type == "APPLICATION"
    assert result.confidence == "HIGH"
    assert result.evidence[0].kind == "THREAD_ASSOCIATION"


def test_ambiguous_thread_prior_matches_do_not_produce_false_association():
    """More than one distinct job_id among thread prior matches means the
    thread's own history is itself inconsistent — must not be trusted as
    a decisive association; falls through to composite scoring.
    """
    jobs = [_job(1, "Python Developer", "Acme GmbH", url="https://acme.example.com")]
    result = match_email_to_job(
        subject="Re: your application",
        body_plain="Acme GmbH follow-up",
        from_address="hr@acme.example.com",
        job_candidates=jobs,
        thread_prior_matches=[
            ThreadPriorMatch(job_id=1, match_type="APPLICATION"),
            ThreadPriorMatch(job_id=2, match_type="APPLICATION"),
        ],
    )
    assert result.evidence == () or result.evidence[0].kind != "THREAD_ASSOCIATION"


def test_cross_account_isolation_is_caller_responsibility_via_bounded_candidates():
    """The matcher itself has no notion of account_key — isolation is
    enforced by the repository layer only ever supplying job_candidates/
    thread_prior_matches already scoped to the requesting account (see
    test_gmail_analysis_repository.py for the DB-level isolation test).
    This test only documents/locks in that the matcher trusts its inputs
    as already-scoped and does not need its own account parameter.
    """
    import inspect

    from app.services.email_matching import match_email_to_job as fn

    assert "account_key" not in inspect.signature(fn).parameters


def test_free_mail_domain_does_not_produce_false_high_confidence():
    jobs = [_job(1, "Data Engineer", "SomeCo", url="https://someco.example.com")]
    result = match_email_to_job(
        subject="hi",
        body_plain="just checking in about the role",
        from_address="someone@gmail.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.confidence != "HIGH"
    assert not any(item.kind == "DOMAIN_COMPANY_MATCH" for item in result.evidence)


def test_normalize_company_name_strips_legal_suffix_conservatively():
    assert normalize_company_name("ABC GmbH") == normalize_company_name("ABC")
    assert normalize_company_name("Acme AG") != normalize_company_name("Beta AG")


def test_extract_sender_domain_handles_malformed_input():
    assert extract_sender_domain(None) is None
    assert extract_sender_domain("not-an-email") is None
    assert (
        extract_sender_domain("Person <person@Example.COM>".split("<")[-1].rstrip(">"))
        == "example.com"
    )


def test_extract_company_domain_strips_www():
    assert extract_company_domain("https://www.acme.example.com/jobs/1") == "acme.example.com"
    assert extract_company_domain("acme.example.com") == "acme.example.com"


def test_evidence_and_candidate_lists_are_bounded():
    from app.services.email_matching import MATCH_CANDIDATE_LIST_MAX_ITEMS

    jobs = [
        _job(i, "Python Developer", f"Company{i}", url=f"https://c{i}.example.com")
        for i in range(1, 20)
    ]
    result = match_email_to_job(
        subject="Bewerbung",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        from_address="hr@ats.example.com",
        job_candidates=jobs,
        thread_prior_matches=[],
    )
    assert result.match_type == "AMBIGUOUS"
    assert len(result.candidates) <= MATCH_CANDIDATE_LIST_MAX_ITEMS
