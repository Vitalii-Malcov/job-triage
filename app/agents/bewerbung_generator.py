"""Pin/staleness verification for Bewerbung generation (Stage 6D).

**6D consumes 6C, it does not re-derive candidate facts (spec section 2).**
Evidence-packet construction and rendering live in
`app.agents.bewerbung_renderer` — this module owns only the "is this
generation request still valid?" checks: does the pinned CV draft belong
to this job, is the candidate profile still at the version the draft was
generated against, has the job's own content fingerprint changed, and is
the CV draft's pinned match still self-consistent.

**BEWERBUNG_GENERATOR_VERSION.** `"v1"` — bumped whenever evidence-packet
construction or rendering rules in `app.agents.bewerbung_renderer` change
in a way that would produce a different draft for the same inputs (mirrors
`app.agents.cv_adapter.CV_ADAPTER_VERSION`'s rationale exactly).
"""

BEWERBUNG_GENERATOR_VERSION = "v1"


class BewerbungCVDraftNotFoundError(Exception):
    """`cv_draft_id` does not correspond to any persisted
    `CandidateCVDraftRecord` (spec section 3) — mapped to 404 by
    `app/api/routes.py`.
    """

    def __init__(self, cv_draft_id: int) -> None:
        self.cv_draft_id = cv_draft_id
        super().__init__(f"No CV draft found with id={cv_draft_id}.")


class BewerbungCVDraftJobMismatchError(Exception):
    """`cv_draft_id` exists but its `job_id` does not equal the job_id in
    the request URL (spec section 3). Mapped to 422, not 409 — mirrors
    `app.agents.cv_adapter.CVDraftMatchJobMismatchError`'s exact
    422-vs-409 rationale: the request itself names a structurally
    inconsistent job_id + cv_draft_id combination, never a state that
    merely changed since the draft was generated.
    """

    def __init__(self, cv_draft_id: int, job_id: int) -> None:
        self.cv_draft_id = cv_draft_id
        self.job_id = job_id
        super().__init__(f"CV draft {cv_draft_id} does not belong to job {job_id}.")


class BewerbungProfileChangedError(Exception):
    """The current `CandidateProfileRecord.profile_version` no longer
    equals the pinned CV draft's `candidate_profile_version` (spec section
    5) — 409. Carries only version numbers (technical metadata), never
    profile content.
    """

    def __init__(self, cv_draft_profile_version: int, current_profile_version: int) -> None:
        self.cv_draft_profile_version = cv_draft_profile_version
        self.current_profile_version = current_profile_version
        super().__init__("Candidate profile changed since this CV draft was generated.")


class BewerbungJobChangedError(Exception):
    """The job's current content fingerprint no longer equals the pinned
    CV draft's `job_snapshot_fingerprint` (spec section 5) — 409. No job
    title, description, or skill list in the message.
    """

    def __init__(self) -> None:
        super().__init__("Job changed since this CV draft was generated.")


class BewerbungMatchNotFoundError(Exception):
    """The pinned CV draft's `match_id` no longer resolves to a persisted
    `CandidateJobMatchRecord`. Should be unreachable — no code path in this
    project ever deletes match rows — but guarded defensively, mirroring
    `app.db.candidate_cv_draft_repository.CandidateCVDraftConsistencyError`'s
    "should never happen, but fail loudly if it does" stance. Mapped to 500.
    """

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(f"CV draft references match_id={match_id}, which no longer exists.")


class BewerbungMatchInconsistentError(Exception):
    """The pinned CV draft's own traceability copies
    (`candidate_profile_version`/`job_snapshot_fingerprint`/
    `match_algorithm_version`) no longer agree with the match record they
    were copied from (spec section 46) — should be unreachable, since
    nothing in this project mutates a persisted match or CV draft after
    creation, but checked defensively before ever invoking a provider.
    Mapped to 500, like `BewerbungMatchNotFoundError`: this indicates a
    data-integrity bug, not a normal staleness case (those are already
    covered by `BewerbungProfileChangedError`/`BewerbungJobChangedError`).
    """

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(f"Match {match_id} is inconsistent with its pinned CV draft.")
