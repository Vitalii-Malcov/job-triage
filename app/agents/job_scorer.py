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
    # CSP-002: "REST"/"rest" and "REST API"/"rest-api" are the same
    # underlying evidence signal (a RESTful HTTP API skill) -- without
    # this, "REST API" normalized to "rest-api" while bare "REST"
    # normalized to "rest", so the two never matched each other despite
    # naming the same skill. Deliberately narrow: only these two textual
    # variants of this one skill, no fuzzy/semantic matching.
    "rest api": "rest",
    "rest-api": "rest",
}


def normalize_skill(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return ALIASES.get(normalized, normalized.replace(" ", "-"))


class JobScorer:
    """Deterministic weighted scorer with aliases and description evidence."""

    def __init__(self, profile_skills: set[str]) -> None:
        self.profile_skills = {normalize_skill(skill) for skill in profile_skills}

    def score(self, job: Job) -> JobScore:
        # H1 fix (Astra Stage 12 audit, round 2): `job.skills` is the
        # ingestion UNION of must-have + nice-to-have + any source-provided
        # / unclassified skills (see app.services.collector_runner's
        # `all_skills = set(job.skills) | must | nice`), and
        # `app.api.routes`' authenticated /jobs/score path accepts a `Job`
        # with arbitrary caller-supplied `skills` too. It is therefore NOT
        # requirement evidence: nothing about a skill appearing in it says
        # the posting demands that skill.
        #
        # This used to fall back to `job.skills` as the must-have set
        # whenever BOTH explicit categories were empty, justified as
        # "pre-must/nice-split legacy data". Astra's round-2 re-review
        # showed that shape is not proof of historical provenance -- a
        # FRESH posting whose description merely MENTIONS technologies
        # ("Our platform uses Python and PostgreSQL to serve customers")
        # extracts to must=[], nice=[], skill_source="description_extracted"
        # while source skills survive the ingestion union, and the fallback
        # then promoted those descriptive mentions to mandatory evidence
        # and reached 90/APPLY.
        #
        # There is no trustworthy discriminator available to separate the
        # two cases: `skill_source` is NULL both for pre-enrichment
        # historical rows (the column was added nullable by migration
        # c4e72b1a8d9f) and for any current caller that simply omits it,
        # and nothing else on `Job`/`JobRecord` records ingestion-schema
        # provenance. Per the remediation direction, the unsafe fallback
        # is therefore REMOVED outright rather than gated on a guess:
        # unclassified source skills still contribute to `matched_skills`/
        # `missing_skills` and to `data_confidence` below (descriptive
        # context), but they can never become must-have evidence, so they
        # can never manufacture must_score=1.0 or clear
        # MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY. A job with no extracted
        # must-have requirements now scores on the neutral
        # "no evidence" path (must_score=0.5) exactly like any other
        # posting whose requirements could not be determined.
        source_skills = {normalize_skill(skill) for skill in job.skills}
        must = {normalize_skill(skill) for skill in job.must_have_skills}
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
            source_skills | must | nice,
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

        matched = (must | nice | source_skills) & self.profile_skills
        missing = (must | nice | source_skills) - self.profile_skills
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
