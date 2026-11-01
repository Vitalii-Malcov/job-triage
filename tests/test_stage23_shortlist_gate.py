"""Stage 23: shortlist quality gate (APPLY_CANDIDATE / REVIEW / SKIP)."""

from app.services.job_fit_assessment import Criterion, FitAssessment
from app.services.shortlist_gate import evaluate


def _assessment(**statuses: str) -> FitAssessment:
    base = {
        "skills": "STRONG",
        "role": "PRIMARY",
        "domain": "OK",
        "experience": "OK",
        "language": "OK",
        "location": "PASS",
        "entry_level": "ENTRY_LEVEL",
        "education": "NO_REQUIREMENT",
        "duplicate": "CLEAR",
    }
    base.update(statuses)
    a = FitAssessment(job_id=1, company="X GmbH", title="Junior Python Entwickler")
    a.criteria = [Criterion(n, s, f"{n} {s}") for n, s in base.items()]
    return a


def test_clean_match_is_apply_candidate_with_liveness_question():
    result = evaluate(_assessment())
    assert result.verdict == "APPLY_CANDIDATE"
    assert any("still online" in q for q in result.questions)


def test_gone_vacancy_is_skip():
    assert evaluate(_assessment(), liveness="GONE").verdict == "SKIP"


def test_off_domain_is_skip_with_reason():
    result = evaluate(_assessment(domain="OFF_DOMAIN"))
    assert result.verdict == "SKIP"
    assert any("domain OFF_DOMAIN" in r for r in result.reasons)


def test_missing_skills_is_review_never_skip():
    result = evaluate(_assessment(skills="NOT_EVALUATED"))
    assert result.verdict == "REVIEW"
    assert result.questions


def test_unverified_location_is_review():
    result = evaluate(_assessment(location="DISTANCE_UNVERIFIED"), liveness="ACTIVE")
    assert result.verdict == "REVIEW"


def test_non_target_role_is_review():
    result = evaluate(_assessment(role="OTHER"), liveness="ACTIVE")
    assert result.verdict == "REVIEW"
    assert any("role" in r for r in result.reasons)


def test_training_programme_in_off_target_domain_is_review():
    result = evaluate(_assessment(domain="OFF_DOMAIN_TRAINING"), liveness="ACTIVE")
    assert result.verdict == "REVIEW"
    assert any("domain OFF_DOMAIN_TRAINING" in q for q in result.questions)


def test_experienced_posting_is_not_apply_candidate():
    result = evaluate(_assessment(entry_level="EXPERIENCED"), liveness="ACTIVE")
    assert result.verdict in {"REVIEW", "SKIP"}
