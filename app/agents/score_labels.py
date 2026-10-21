"""Display labels for the two DIFFERENT scores this project computes
(Stage 14A, "83 vs 80" investigation).

They are intentionally different numbers, each persisted once and reused:

1. **Stellen-Score** -- `JobRecord.score`, written at ingestion by
   `app.agents.job_scorer.JobScorer` (via `app.agents.job_score_evaluator`).
   A triage score over the posting: must-have skill overlap (70 %),
   nice-to-have overlap (20 %, neutral 0.5 when the posting lists none) and
   candidate-skill mentions anywhere in the posting text (10 %). It only
   knows the candidate's trusted skill NAMES.
2. **Kandidaten-Match** -- `CandidateJobMatchRecord.overall_score`, written
   by `app.agents.candidate_job_matcher.compute_match` (ALGORITHM_VERSION
   "v1") when a Bewerbung is prepared. Coverage of the posting's REQUIRED
   (60 %) and PREFERRED (20 %, 100 when none) requirements by trusted
   candidate evidence, plus how much of the required-skill coverage is
   backed by a trusted experience/project entry (20 %).

Example (the live E362/B case): must-have {python, git, rest}, no
nice-to-haves, 3 of 11 profile skills mentioned in the text, no trusted
experience/project -> Stellen-Score round(70 + 10 + 2.7) = 83, Kandidaten-
Match round(60 + 20 + 0) = 80. Neither value is recomputed for display; each
surface reads its own persisted value. These labels exist so no surface
shows either one as a bare, unqualified "Match".
"""

VACANCY_SCORE_LABEL = "Stellen-Score (Vorauswahl)"
CANDIDATE_MATCH_LABEL = "Kandidaten-Match (belegte Profil-Nachweise)"
OPEN_REQUIREMENTS_QUALIFIER = "kein vollständiger Stellen-Fit"


def candidate_match_lines(score: int, *, evidenced: int, open_count: int) -> list[str]:
    """V1 quality patch: the Kandidaten-Match only counts CONFIRMED profile
    evidence, so a 100/100 next to an unverified essential requirement (e.g.
    a completed degree) read as a perfect fit. Whenever any requirement is
    partial, missing or unclear, the score line says so and the evidence
    coverage and open requirements are shown next to it. The score itself
    is never altered."""
    if open_count == 0:
        return [f"{CANDIDATE_MATCH_LABEL}: {score}/100"]
    return [
        f"{CANDIDATE_MATCH_LABEL}: {score}/100 – {OPEN_REQUIREMENTS_QUALIFIER}",
        f"Bestätigte Profil-Nachweise: {evidenced}/{evidenced + open_count} Anforderungen",
        f"Offene Anforderungen: {open_count} (teilweise, nicht belegt oder unklar)",
    ]


SCORE_DIFFERENCE_NOTE = (
    "Kandidaten-Match ist nicht der Stellen-Score der Vorauswahl: er zählt nur "
    "bestätigte Profil-Nachweise (inkl. Berufserfahrung/Projekte)."
)
