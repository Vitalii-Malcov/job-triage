"""The single shared "is this persisted Bewerbung draft still current?"
identity (Stage 8C automation reuse + Stage 9B Telegram preparation).

Moved here from `app.services.automation_shortlist._bewerbung_is_current`
(not copied) so both callers make the SAME reuse decision, and strengthened
(Stage 9B, Codex architecture review section 6):

- Every original pin is still compared: the exact CV draft, its match, the
  candidate profile version, the job snapshot fingerprint, the matcher /
  CV-adapter / generator versions and the deterministic provider.
- The draft and the CV draft must both belong to `job`, and the CV draft's
  job snapshot fingerprint must still equal the job's CURRENT fingerprint.
- **Exact letter rendering context.** `compute_job_snapshot_fingerprint`
  casefolds the title and does not include the company, but the letter's
  subject and opening render both verbatim. A company-only correction or a
  title case change would leave every old pin equal while the old letter
  still names the previous text. The draft's persisted
  `BewerbungJobContext` must therefore equal the job's current title and
  company exactly. Legacy drafts without that context (pre-Stage-9B) are
  never current -- the context is never inferred by parsing prose.

Profile currency is the caller's responsibility: both callers pass a CV
draft they just prepared against the current profile (Stage 9B also
re-verifies the profile version when it publishes).
"""

import json

from app.agents.bewerbung_generator import BEWERBUNG_GENERATOR_VERSION
from app.db.candidate_job_match_repository import compute_job_snapshot_fingerprint
from app.db.models import BewerbungDraftRecord, CandidateCVDraftRecord, JobRecord
from app.providers.bewerbung.deterministic import PROVIDER_NAME as DETERMINISTIC_BEWERBUNG_PROVIDER

__all__ = ["bewerbung_draft_is_current", "persisted_job_context"]


def persisted_job_context(record: BewerbungDraftRecord) -> tuple[str, str] | None:
    """The (title, company) a draft was rendered with, read from its own
    persisted JSON -- None for legacy rows without it or malformed JSON."""
    try:
        context = json.loads(record.draft_json).get("job_context")
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(context, dict):
        return None
    title = context.get("title")
    company = context.get("company")
    if not isinstance(title, str) or not isinstance(company, str):
        return None
    return title, company


def bewerbung_draft_is_current(
    existing: BewerbungDraftRecord | None,
    cv_draft_record: CandidateCVDraftRecord | None,
    job: JobRecord,
) -> bool:
    """True only if `existing` was generated from exactly `cv_draft_record`
    (and its match/profile/fingerprint/version pins), by the current
    generator version and the deterministic provider, for `job` as it is
    displayed RIGHT NOW (exact title and company)."""
    if existing is None or cv_draft_record is None:
        return False
    if existing.job_id != job.id or cv_draft_record.job_id != job.id:
        return False
    if cv_draft_record.job_snapshot_fingerprint != compute_job_snapshot_fingerprint(job):
        return False
    pins_match = (
        existing.cv_draft_id == cv_draft_record.id
        and existing.match_id == cv_draft_record.match_id
        and existing.candidate_profile_version == cv_draft_record.candidate_profile_version
        and existing.job_snapshot_fingerprint == cv_draft_record.job_snapshot_fingerprint
        and existing.match_algorithm_version == cv_draft_record.match_algorithm_version
        and existing.cv_adapter_version == cv_draft_record.cv_adapter_version
        and existing.bewerbung_generator_version == BEWERBUNG_GENERATOR_VERSION
        and existing.provider == DETERMINISTIC_BEWERBUNG_PROVIDER
    )
    if not pins_match:
        return False
    return persisted_job_context(existing) == (job.title, job.company)
