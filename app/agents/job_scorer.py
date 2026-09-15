import re

from app.agents.data_confidence import calculate_data_confidence
from app.models.job import Job, JobScore

MINIMUM_DECISION_CONFIDENCE = 0.45

# Stage 10 shadow-mode pilot finding: a posting whose extracted must-have
# set has fewer than this many entries can trivially reach must_score=1.0
# from a single coincidental match (e.g. a training-course description
# that only literally says "Python" once) -- that is too little structured
# requirement evidence to justify an automatic APPLY, regardless of how
# high the resulting numeric score is. See the recommendation logic below
# ("missing evidence must never manufacture high confidence").
MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY = 2

ALIASES = {
    "fast api": "fastapi",
    "fast-api": "fastapi",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mongo": "mongodb",
    "mongo db": "mongodb",
    "ci/cd": "cicd",
    "ci cd": "cicd",
    "github actions": "github-actions",
}


def normalize_skill(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return ALIASES.get(normalized, normalized.replace(" ", "-"))


class JobScorer:
    """Deterministic weighted scorer with aliases and description evidence."""

    def __init__(self, profile_skills: set[str]) -> None:
        self.profile_skills = {normalize_skill(skill) for skill in profile_skills}

    def score(self, job: Job) -> JobScore:
        legacy = {normalize_skill(skill) for skill in job.skills}
        must = {normalize_skill(skill) for skill in job.must_have_skills} or legacy
        nice = {normalize_skill(skill) for skill in job.nice_to_have_skills}

        matched_must = must & self.profile_skills
        missing_must = must - self.profile_skills
        matched_nice = nice & self.profile_skills

        if must:
            must_score = len(matched_must) / len(must)
        else:
            must_score = 0.5
        # Stage 10 fix: an empty nice-to-have set is an ABSENCE of
        # evidence, not proof every (nonexistent) nice-to-have was
        # satisfied -- defaulting to 1.0 here silently added a free 20%
        # (this term's full weight) to every sparsely-extracted posting's
        # score. Neutral (0.5), matching must_score's own "no evidence"
        # default immediately above, not full credit.
        nice_score = len(matched_nice) / len(nice) if nice else 0.5

        text = f"{job.title} {job.description}".casefold()
        description_hits = sum(
            1 for skill in self.profile_skills if skill.replace("-", " ") in text or skill in text
        )
        description_score = min(description_hits / max(len(self.profile_skills), 1), 1.0)

        raw_score = (must_score * 0.70) + (nice_score * 0.20) + (description_score * 0.10)
        score = round(raw_score * 100)

        data_confidence = calculate_data_confidence(
            job.description,
            legacy | must | nice,
        )

        if data_confidence < MINIMUM_DECISION_CONFIDENCE:
            recommendation = "NEEDS_ENRICHMENT"
        elif missing_must and must_score < 0.6:
            recommendation = "SKIP"
        elif score >= 80 and len(must) < MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY:
            # Missing evidence must never manufacture high confidence: a
            # must-have set this sparse cannot support an automatic APPLY
            # no matter how high the numeric score is (see
            # MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY above) -- downgrade to
            # MAYBE, a human still reviews it.
            recommendation = "MAYBE"
        elif score >= 80:
            recommendation = "APPLY"
        elif score >= 60:
            recommendation = "MAYBE"
        else:
            recommendation = "SKIP"

        matched = (must | nice | legacy) & self.profile_skills
        missing = (must | nice | legacy) - self.profile_skills
        return JobScore(
            score=score,
            matched_skills=sorted(matched),
            missing_skills=sorted(missing),
            matched_must_have=sorted(matched_must),
            missing_must_have=sorted(missing_must),
            matched_nice_to_have=sorted(matched_nice),
            recommendation=recommendation,
            data_confidence=data_confidence,
        )
