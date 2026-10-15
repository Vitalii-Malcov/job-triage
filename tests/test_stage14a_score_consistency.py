"""Stage 14A regression: the live "Match 83%" (vacancy card) vs.
"Kandidaten-Match 80" (Bewerbung preview) mismatch.

Root cause: two different, individually persisted scores were both shown
as a bare "Match". `JobRecord.score` comes from JobScorer (70/20/10 over
must/nice/text mentions); `CandidateJobMatch.overall_score` comes from
compute_match (60/20/20 over required/preferred/experience-backing). This
test reproduces the exact 83/80 pair from the E362/B shape with synthetic
data and pins that every surface labels which score it shows.
"""

import json
from datetime import UTC, datetime

from app.agents.candidate_job_matcher import JobMatchInput, compute_match
from app.agents.cv_adapter import compute_cv_draft
from app.agents.job_scorer import JobScorer
from app.agents.score_labels import (
    CANDIDATE_MATCH_LABEL,
    SCORE_DIFFERENCE_NOTE,
    VACANCY_SCORE_LABEL,
)
from app.db.models import JobRecord
from app.models.bewerbung import (
    AllowedClaim,
    BewerbungDraft,
    BewerbungJobContext,
    BewerbungParagraph,
    BewerbungProviderPlan,
)
from app.models.candidate_job_match import CandidateJobMatch
from app.models.candidate_profile import CandidateExperience, CandidateProfile, CandidateSkill
from app.models.cv_draft import TailoredCVDraft
from app.models.job import Job
from app.services.telegram_bewerbung_preview import render_summary
from app.services.telegram_vacancy_feed import (
    build_vacancy_card,
    render_vacancy_card,
    render_vacancy_details,
)

# 11 trusted skills, mirroring the shape of the pilot profile.
_SKILLS = [
    "Python",
    "FastAPI",
    "Flask",
    "SQLAlchemy",
    "PostgreSQL",
    "MySQL",
    "MongoDB",
    "REST API",
    "Git",
    "Pytest",
    "Docker",
]
_MUST = ["git", "python", "rest"]
# Mentions exactly python, git and rest (3/11) and nothing else that would
# substring-match a profile skill.
_DESCRIPTION = "Sie entwickeln KI-Loesungen mit Python, Git und REST fuer unsere Kunden."
_TITLE = "AI Engineer / KI-Entwickler (m/w/d)"


def _profile() -> CandidateProfile:
    return CandidateProfile(
        id=1,
        profile_version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        skills=[CandidateSkill(id=i + 1, name=name) for i, name in enumerate(_SKILLS)],
    )


def _vacancy_score() -> int:
    job = Job(
        source="bundesagentur",
        title=_TITLE,
        company="Example Informatik",
        url="https://example.com/job/1",
        description=_DESCRIPTION,
        skills=_MUST,
        must_have_skills=_MUST,
    )
    return JobScorer(set(_SKILLS)).score(job).score


def _match() -> CandidateJobMatch:
    data = compute_match(
        JobMatchInput(job_id=1, title=_TITLE, description=_DESCRIPTION, must_have_skills=_MUST),
        _profile(),
        company_research_id=None,
    )
    return CandidateJobMatch(id=1, created_at=datetime.now(UTC), **data.model_dump())


def _record(score: int) -> JobRecord:
    return JobRecord(
        id=1,
        fingerprint="f" * 64,
        source="bundesagentur",
        title=_TITLE,
        company="Example Informatik",
        location="Frankfurt am Main",
        url="https://example.com/job/1",
        description=_DESCRIPTION,
        skills_json=json.dumps(_MUST),
        data_confidence=0.85,
        skill_source="description_extracted",
        must_have_skills_json=json.dumps(_MUST),
        nice_to_have_skills_json="[]",
        posting_type="ARBEIT",
        score=score,
        recommendation="APPLY",
        status="NEW",
    )


def _cv(match: CandidateJobMatch) -> TailoredCVDraft:
    data = compute_cv_draft(_profile(), match)
    return TailoredCVDraft(id=1, created_at=datetime.now(UTC), **data.model_dump())


def _letter() -> BewerbungDraft:
    return BewerbungDraft(
        id=1,
        created_at=datetime.now(UTC),
        job_id=1,
        cv_draft_id=1,
        match_id=1,
        candidate_profile_version=1,
        job_snapshot_fingerprint="f" * 64,
        match_algorithm_version="v1",
        cv_adapter_version="v2",
        bewerbung_generator_version="v3",
        provider="deterministic",
        subject=f"Bewerbung als {_TITLE}",
        salutation="Sehr geehrte Damen und Herren,",
        opening="x",
        body_paragraphs=[BewerbungParagraph(text="y", source_claim_ids=["candidate_skill:1"])],
        closing="z",
        signature_name=None,
        plan=BewerbungProviderPlan(
            opening_style="ROLE_INTEREST",
            paragraphs=[{"kind": "EVIDENCE", "claim_ids": ["candidate_skill:1"]}],
            closing_style="INTERVIEW_INTEREST",
        ),
        claims=[
            AllowedClaim(
                id="candidate_skill:1", claim="Python", source_entity="candidate_skill", source_id=1
            )
        ],
        job_context=BewerbungJobContext(title=_TITLE, company="Example Informatik"),
    )


def test_reproduces_live_83_vs_80_pair_from_two_algorithms():
    match = _match()
    assert _vacancy_score() == 83  # 0.70*3/3 + 0.20*0.5 + 0.10*3/11
    assert match.overall_score == 80  # 0.6*100 + 0.2*100 + 0.2*0
    assert match.required_skill_score == 100
    assert match.preferred_skill_score == 100  # no PREFERRED requirement
    assert match.experience_support_score == 0  # no trusted experience/project


def test_trusted_experience_closes_the_gap_only_on_the_candidate_match():
    """The two scores move independently: structured evidence raises the
    candidate match, JobScorer never sees it."""
    profile = _profile()
    profile.experiences = [
        CandidateExperience(id=1, company="X", job_title="Dev", technologies=["Python", "Git"])
    ]
    data = compute_match(
        JobMatchInput(job_id=1, title=_TITLE, description=_DESCRIPTION, must_have_skills=_MUST),
        profile,
        company_research_id=None,
    )
    assert data.experience_support_score == 33
    assert data.overall_score == 87
    assert _vacancy_score() == 83


def test_vacancy_card_and_details_label_the_persisted_vacancy_score():
    record = _record(_vacancy_score())
    card_text = render_vacancy_card(build_vacancy_card(record, _SKILLS))
    details = render_vacancy_details(record)
    assert f"{VACANCY_SCORE_LABEL}: 83/100" in card_text
    assert f"{VACANCY_SCORE_LABEL}: 83/100 (APPLY)" in details
    for text in (card_text, details):
        assert "Match: 83%" not in text
        assert CANDIDATE_MATCH_LABEL not in text


def test_bewerbung_preview_labels_candidate_match_and_explains_difference():
    match = _match()
    text = render_summary(_letter(), _cv(match), match)
    assert f"{CANDIDATE_MATCH_LABEL}: 80/100" in text
    assert SCORE_DIFFERENCE_NOTE in text
    assert VACANCY_SCORE_LABEL not in text
    assert "83" not in text  # the preview never recomputes/borrows the vacancy score


def test_labels_are_distinct():
    assert VACANCY_SCORE_LABEL != CANDIDATE_MATCH_LABEL
    assert "Match" not in VACANCY_SCORE_LABEL
