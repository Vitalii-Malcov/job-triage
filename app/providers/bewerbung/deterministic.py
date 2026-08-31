"""Stage 6D's safe default and only-shipped provider: deterministic
structural planning with zero network calls and zero natural-language
"understanding" of its input.

Unlike the pre-blocker-fix version of this module, this provider does not
author any candidate-facing text at all — it only decides *which*
`evidence.allowed_claims` ids to reference and picks bounded
opening/closing style enums. `app.agents.bewerbung_renderer.render_draft`
is the only code that ever turns those choices into actual German
sentences.

Because this provider never interprets free text as instructions — it does
plain field/list lookups over `BewerbungEvidencePacket.allowed_claims`, and
never reads `.job.description`'s content at all — a prompt-injection
attempt embedded in a job description (spec section 43/45) has no code
path through which it could ever influence this provider's output. See
`tests/test_bewerbung_renderer.py` for a test that proves this by
injecting adversarial instruction text into `evidence.job.description` and
asserting the resulting plan is unaffected.

Used both as the production default (no paid/network dependency required
to ship Stage 6D — spec section 8's "acceptable to leave [a real LLM]
provider not configured rather than forcing a vendor integration badly")
and, unmodified, as the offline test provider (spec section 8's "tests
must not require internet or paid API calls") — this project intentionally
does not maintain two divergent "prod" vs. "test" implementations of the
same contract.
"""

from app.models.bewerbung import BewerbungEvidencePacket
from app.providers.bewerbung.base import BewerbungProvider

PROVIDER_NAME = "deterministic"

_MAX_SKILLS_PER_PARAGRAPH = 4


class DeterministicBewerbungProvider(BewerbungProvider):
    """Picks a small, sensible set of `allowed_claims` ids to group into
    paragraphs — never generates a sentence itself; that is
    `app.agents.bewerbung_renderer.render_draft`'s job.
    """

    name = PROVIDER_NAME

    async def generate_plan(self, evidence: BewerbungEvidencePacket) -> dict:
        skill_ids = [
            claim.id
            for claim in evidence.allowed_claims
            if claim.source_entity == "candidate_skill"
        ][:_MAX_SKILLS_PER_PARAGRAPH]
        highlight_ids = [
            claim.id
            for claim in evidence.allowed_claims
            if claim.source_entity in ("candidate_experience", "candidate_project")
        ][:1]
        language_ids = [
            claim.id
            for claim in evidence.allowed_claims
            if claim.source_entity == "candidate_language"
        ][:1]

        paragraphs: list[dict] = []
        if skill_ids:
            paragraphs.append({"kind": "EVIDENCE", "claim_ids": skill_ids})
        if highlight_ids:
            paragraphs.append({"kind": "EVIDENCE", "claim_ids": highlight_ids})
        if language_ids:
            paragraphs.append({"kind": "EVIDENCE", "claim_ids": language_ids})

        if not paragraphs:
            # No candidate evidence to reference at all — the only legal
            # fallback is a GENERIC paragraph (no claim ids), never an
            # EVIDENCE paragraph with nothing to point at.
            paragraphs.append({"kind": "GENERIC", "claim_ids": []})

        return {
            "opening_style": "ROLE_INTEREST" if skill_ids else "MATCH_FOCUS",
            "paragraphs": paragraphs,
            "closing_style": "INTERVIEW_INTEREST",
        }
