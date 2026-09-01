"""Deterministic evidence-based matching of one inbound email to a
tracked `JobRecord` (Stage 7B).

**Job vs. Application.** This project has no separate `ApplicationRecord`
— `JobRecord` (app.db.models.JobRecord) carries both the job posting AND
its `status` (`ApplicationStatus`). "Matched to an application" here
means matched to a `JobRecord` whose `status` indicates the candidate has
actually acted on it (APPLIED/INTERVIEW/REJECTED/OFFER/WITHDRAWN, see
`_APPLICATION_STATUSES`); "matched to a job only" means matched to a
`JobRecord` still at NEW/SAVED. See `_match_type_for_status`.

**No arbitrary winner (spec: "Do not match by guessing").** Every
candidate `JobRecord` is scored independently and deterministically; if
more than one candidate ties for the top score, the result is
`AMBIGUOUS` with every tied candidate returned — never a coin-flip pick
among equals.

**Precedence, implemented via weight design, not branching.** The spec's
required precedence (trusted thread association > exact job reference >
composite company/title/domain/location evidence > ambiguous > unmatched)
is achieved two ways:

1. **Thread association is a hard short-circuit** — evaluated first, and
   if a single distinct job is associated with prior (non-ambiguous)
   analyses in the same trusted `GmailThreadRecord`, that decides the
   match outright; composite scoring for other candidates never runs.
   "Trusted" here relies entirely on Stage 7A's own thread-membership
   guarantee (GMAIL-011): a message that shared an ambiguous/contested
   Message-ID was already routed to its OWN synthetic thread by
   app.db.gmail_repository, so by the time this module sees "these
   messages share a `thread_id`", that grouping has already been vetted
   — this module does not re-inspect `GmailMessageIdClaimRecord.contested`
   itself.
2. **`MATCH_EVIDENCE_WEIGHTS["JOB_REFERENCE"]` is set strictly higher
   than the maximum possible composite score from every OTHER evidence
   kind combined** (see the assertion right after the weight table) —
   so an exact job-reference match always outranks a same-candidate's
   composite company+title+domain+location score, without needing a
   separate code branch to enforce it. The ordinary top-score-wins (with
   exact-tie -> AMBIGUOUS) comparison below applies uniformly to every
   evidence kind, including JOB_REFERENCE and THREAD_ASSOCIATION.

**Weak/generic evidence never reaches HIGH or MEDIUM alone** — every
generic title token (`_GENERIC_TITLE_WORDS`) contributes at most
`GENERIC_TITLE_TOKEN_WEIGHT` each, capped at
`GENERIC_TITLE_TOKEN_MAX_TOKENS` tokens, so the maximum possible
generic-only score stays below `MATCH_CONFIDENCE_MEDIUM_THRESHOLD` (see
the assertion below). Multiple candidates that tie ONLY on generic
overlap (e.g. five different "Python Developer" applications) still
correctly resolve to `AMBIGUOUS`, not to an arbitrarily "first" one —
the spec's own worked example.

**Bounded, always.** The candidate `JobRecord` pool this module scores is
supplied by the caller already bounded (see
`MATCH_CANDIDATE_SCAN_LIMIT` and app/db/gmail_analysis_repository.py's
`get_job_candidates`) — this module never queries a database itself and
has no way to cause an unbounded scan on its own.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

MatchType = Literal["APPLICATION", "JOB_ONLY", "AMBIGUOUS", "UNMATCHED"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]

# A JobRecord at one of these statuses represents a real, acted-on
# application, not merely a tracked posting — see module docstring.
_APPLICATION_STATUSES = frozenset({"APPLIED", "INTERVIEW", "REJECTED", "OFFER", "WITHDRAWN"})

# Bounds enforced by the caller (app/db/gmail_analysis_repository.py) —
# documented here since this module defines what they protect against.
MATCH_CANDIDATE_SCAN_LIMIT = 500
THREAD_ASSOCIATION_SCAN_LIMIT = 50

MATCH_EVIDENCE_MAX_ITEMS = 10
MATCH_CANDIDATE_LIST_MAX_ITEMS = 5
EVIDENCE_FRAGMENT_MAX_LENGTH = 120

MATCH_EVIDENCE_WEIGHTS: dict[str, int] = {
    "THREAD_ASSOCIATION": 100,
    "JOB_REFERENCE": 90,
    "COMPANY_EXACT": 35,
    "DOMAIN_COMPANY_MATCH": 20,
    "LOCATION_OVERLAP": 8,
}
TITLE_TOKEN_WEIGHT = 6
TITLE_TOKEN_MAX_TOKENS = 4
GENERIC_TITLE_TOKEN_WEIGHT = 2
GENERIC_TITLE_TOKEN_MAX_TOKENS = 3

MATCH_CONFIDENCE_HIGH_THRESHOLD = 70
MATCH_CONFIDENCE_MEDIUM_THRESHOLD = 30

_MAX_COMPOSITE_WITHOUT_REFERENCE = (
    MATCH_EVIDENCE_WEIGHTS["COMPANY_EXACT"]
    + MATCH_EVIDENCE_WEIGHTS["DOMAIN_COMPANY_MATCH"]
    + MATCH_EVIDENCE_WEIGHTS["LOCATION_OVERLAP"]
    + TITLE_TOKEN_WEIGHT * TITLE_TOKEN_MAX_TOKENS
)
assert MATCH_EVIDENCE_WEIGHTS["JOB_REFERENCE"] > _MAX_COMPOSITE_WITHOUT_REFERENCE  # noqa: S101
_MAX_GENERIC_ONLY_SCORE = GENERIC_TITLE_TOKEN_WEIGHT * GENERIC_TITLE_TOKEN_MAX_TOKENS
assert _MAX_GENERIC_ONLY_SCORE < MATCH_CONFIDENCE_MEDIUM_THRESHOLD  # noqa: S101

# Generic role/process words (German + English) that must never, by
# themselves, produce a confident match — see module docstring and spec
# section "DO NOT MATCH BY GUESSING". Deliberately a small, explicit,
# reviewable set, not a stopword library.
_GENERIC_TITLE_WORDS = frozenset(
    {
        "developer",
        "entwickler",
        "engineer",
        "ingenieur",
        "software",
        "python",
        "java",
        "javascript",
        "backend",
        "frontend",
        "fullstack",
        "full",
        "stack",
        "senior",
        "junior",
        "mitarbeiter",
        "position",
        "stelle",
        "job",
        "bewerbung",
        "application",
        "specialist",
        "spezialist",
        "consultant",
        "berater",
        "manager",
        "teamlead",
        "lead",
        "intern",
        "praktikant",
        "werkstudent",
        "remote",
    }
)

# A well-known free-mail provider domain is never proof of company
# identity by itself (spec: "Do not trust domain as proof by itself if
# it is generic") — bounded, explicit list, not a heuristic.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "outlook.com",
        "hotmail.com",
        "web.de",
        "gmx.de",
        "gmx.net",
        "yahoo.com",
        "freenet.de",
        "t-online.de",
        "icloud.com",
        "aol.com",
        "live.com",
        "mail.com",
    }
)

_LEGAL_SUFFIX_PATTERN = re.compile(
    r"\b(?:gmbh(?:\s*&\s*co\.?\s*kg)?|mbh|ag|kg|ug|inc\.?|llc|ltd\.?|corp\.?|se|plc|co\.?)\b\.?",
    re.IGNORECASE,
)
_WORD_PATTERN = re.compile(r"[a-zA-ZäöüÄÖÜß0-9+#.]+")

_REFERENCE_PATTERN = re.compile(
    r"\b(?:referenz|kennziffer|ref(?:erenz)?(?:[-\s]?nr\.?)?|job[-\s]?id|"
    r"stellen[-\s]?(?:nr\.?|id)|vacancy[-\s]?id|requisition[-\s]?id)"
    r"[:\s#]*([A-Za-z0-9][A-Za-z0-9\-/]{2,19})",
    re.IGNORECASE,
)
_URL_NUMERIC_ID_PATTERN = re.compile(r"/(\d{4,10})(?:[/?]|$)")


@dataclass(frozen=True)
class JobCandidate:
    """The bounded subset of a JobRecord's fields this module scores
    against — deliberately excludes `description` (kept out of the
    scoring/evidence surface to bound both compute cost per candidate and
    the risk of pulling large free-text bodies into evidence).
    """

    job_id: int
    title: str
    company: str
    location: str
    url: str
    status: str


@dataclass(frozen=True)
class ThreadPriorMatch:
    """One prior analysis's match outcome for an earlier message in the
    SAME (already Stage-7A-vetted) thread — see module docstring for why
    this module trusts `thread_id` grouping as-is.
    """

    job_id: int
    match_type: MatchType


@dataclass(frozen=True)
class MatchEvidenceItem:
    kind: str
    value: str
    weight: int


@dataclass(frozen=True)
class CandidateMatch:
    job_id: int
    score: int
    evidence: tuple[MatchEvidenceItem, ...]


@dataclass(frozen=True)
class EmailMatchResult:
    match_type: MatchType
    matched_job_id: int | None
    confidence: Confidence
    score: int
    evidence: tuple[MatchEvidenceItem, ...]
    candidates: tuple[CandidateMatch, ...]


def _match_type_for_status(status: str) -> MatchType:
    return "APPLICATION" if status in _APPLICATION_STATUSES else "JOB_ONLY"


def _truncate(fragment: str) -> str:
    fragment = fragment.strip()
    if len(fragment) <= EVIDENCE_FRAGMENT_MAX_LENGTH:
        return fragment
    return fragment[:EVIDENCE_FRAGMENT_MAX_LENGTH].rstrip() + "..."


def normalize_company_name(name: str) -> str:
    """Casefold + strip common legal-form suffixes + collapse whitespace.
    Deliberately conservative (spec: "Be conservative... generic words
    must not create matches") — only a small, explicit set of legal
    suffix tokens is stripped; nothing else is altered. "ABC GmbH" and
    "ABC" normalize to the same identity; two genuinely different company
    names never collide merely because both are lowercase.
    """
    normalized = name.casefold()
    normalized = _LEGAL_SUFFIX_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def extract_sender_domain(from_address: str | None) -> str | None:
    """Safe sender-domain parsing — lowercased, handles a display-name
    prefix already stripped by the mail parser (Stage 7A's
    `from_address` is address-only, see ParsedGmailMessage), and returns
    None for anything malformed rather than guessing.
    """
    if not from_address or "@" not in from_address:
        return None
    domain = from_address.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def extract_company_domain(url: str) -> str | None:
    """Safe URL-host parsing for a JobRecord's own `url` — never fetched,
    only parsed as a string (spec: "Collectors... must never make
    outbound requests to links" — this module makes none either).
    Strips a leading `www.` subdomain only; any other subdomain is kept
    as-is (conservative — a genuinely different subdomain may be a
    genuinely different posting source).
    """
    candidate = url if "://" in url else f"//{url}"
    try:
        host = (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _title_tokens(title: str) -> set[str]:
    return {token.casefold() for token in _WORD_PATTERN.findall(title) if len(token) > 1}


def extract_reference_tokens(text: str, url: str = "") -> frozenset[str]:
    """Bounded, best-effort extraction of explicit job/application
    reference identifiers (spec: "explicit job reference / vacancy ID").
    Looks for an explicit labelled reference ("Referenz-Nr: ABC-123",
    "Job ID: 4821", "Kennziffer XYZ") in `text`, plus a bare numeric path
    segment in `url` (common in ATS URLs, e.g. ".../job/482173"). Returns
    normalized (uppercased) tokens; matching is exact-token, never fuzzy
    — an accidental substring collision must not fabricate evidence.
    """
    tokens = {match.group(1).upper() for match in _REFERENCE_PATTERN.finditer(text)}
    if url:
        tokens.update(match.group(1) for match in _URL_NUMERIC_ID_PATTERN.finditer(url))
    return frozenset(tokens)


def _score_candidate(
    job: JobCandidate,
    *,
    normalized_email_text: str,
    email_text: str,
    email_tokens: set[str],
    email_reference_tokens: frozenset[str],
    sender_domain: str | None,
) -> tuple[int, list[MatchEvidenceItem]]:
    score = 0
    evidence: list[MatchEvidenceItem] = []

    normalized_company = normalize_company_name(job.company)
    if len(normalized_company) >= 3 and f" {normalized_company} " in f" {normalized_email_text} ":
        weight = MATCH_EVIDENCE_WEIGHTS["COMPANY_EXACT"]
        score += weight
        evidence.append(MatchEvidenceItem("COMPANY_EXACT", _truncate(job.company), weight))

    job_domain = extract_company_domain(job.url)
    if (
        sender_domain
        and job_domain
        and sender_domain not in FREE_MAIL_DOMAINS
        and sender_domain == job_domain
    ):
        weight = MATCH_EVIDENCE_WEIGHTS["DOMAIN_COMPANY_MATCH"]
        score += weight
        evidence.append(MatchEvidenceItem("DOMAIN_COMPANY_MATCH", sender_domain, weight))

    job_tokens = _title_tokens(job.title)
    distinctive = sorted(
        t for t in job_tokens if t not in _GENERIC_TITLE_WORDS and t in email_tokens
    )
    generic = sorted(t for t in job_tokens if t in _GENERIC_TITLE_WORDS and t in email_tokens)
    if distinctive:
        counted = distinctive[:TITLE_TOKEN_MAX_TOKENS]
        weight = len(counted) * TITLE_TOKEN_WEIGHT
        score += weight
        evidence.append(MatchEvidenceItem("TITLE_TOKEN_OVERLAP", ",".join(counted), weight))
    if generic:
        counted = generic[:GENERIC_TITLE_TOKEN_MAX_TOKENS]
        weight = len(counted) * GENERIC_TITLE_TOKEN_WEIGHT
        score += weight
        evidence.append(MatchEvidenceItem("GENERIC_TITLE_TOKEN_OVERLAP", ",".join(counted), weight))

    location = job.location.strip()
    if location and location.casefold() in email_text.casefold():
        weight = MATCH_EVIDENCE_WEIGHTS["LOCATION_OVERLAP"]
        score += weight
        evidence.append(MatchEvidenceItem("LOCATION_OVERLAP", _truncate(location), weight))

    job_reference_tokens = extract_reference_tokens(f"{job.title}", job.url)
    matched_references = job_reference_tokens & email_reference_tokens
    if matched_references:
        weight = MATCH_EVIDENCE_WEIGHTS["JOB_REFERENCE"]
        score += weight
        evidence.append(MatchEvidenceItem("JOB_REFERENCE", sorted(matched_references)[0], weight))

    return score, evidence


def _confidence_for_score(score: int) -> Confidence:
    if score >= MATCH_CONFIDENCE_HIGH_THRESHOLD:
        return "HIGH"
    if score >= MATCH_CONFIDENCE_MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def match_email_to_job(
    *,
    subject: str,
    body_plain: str,
    from_address: str | None,
    job_candidates: list[JobCandidate],
    thread_prior_matches: list[ThreadPriorMatch],
) -> EmailMatchResult:
    """Pure, deterministic matching — no DB/network access. `job_candidates`
    and `thread_prior_matches` must already be bounded by the caller (see
    module docstring). Returns the single decisive match, an AMBIGUOUS
    result naming every tied top candidate, or UNMATCHED — never an
    arbitrary pick among equally-scored candidates.
    """
    distinct_thread_jobs = {m.job_id for m in thread_prior_matches}
    if len(distinct_thread_jobs) == 1:
        job_id = next(iter(distinct_thread_jobs))
        candidate = next((c for c in job_candidates if c.job_id == job_id), None)
        if candidate is not None:
            match_type = _match_type_for_status(candidate.status)
        else:
            # The associated job fell outside the bounded candidate scan
            # (or no longer exists) — fall back to the trusted prior
            # analysis's own recorded match_type rather than guessing.
            match_type = thread_prior_matches[0].match_type
        weight = MATCH_EVIDENCE_WEIGHTS["THREAD_ASSOCIATION"]
        return EmailMatchResult(
            match_type=match_type,
            matched_job_id=job_id,
            confidence="HIGH",
            score=weight,
            evidence=(MatchEvidenceItem("THREAD_ASSOCIATION", str(job_id), weight),),
            candidates=(),
        )

    email_text = f"{subject}\n{body_plain}"
    normalized_email_text = normalize_company_name(email_text)
    email_tokens = _title_tokens(email_text)
    email_reference_tokens = extract_reference_tokens(email_text)
    sender_domain = extract_sender_domain(from_address)

    scored = [
        (
            job,
            *_score_candidate(
                job,
                normalized_email_text=normalized_email_text,
                email_text=email_text,
                email_tokens=email_tokens,
                email_reference_tokens=email_reference_tokens,
                sender_domain=sender_domain,
            ),
        )
        for job in job_candidates
    ]
    scored = [(job, score, evidence) for job, score, evidence in scored if score > 0]

    if not scored:
        return EmailMatchResult(
            match_type="UNMATCHED",
            matched_job_id=None,
            confidence="LOW",
            score=0,
            evidence=(),
            candidates=(),
        )

    top_score = max(score for _, score, _ in scored)
    top_candidates = [entry for entry in scored if entry[1] == top_score]
    confidence = _confidence_for_score(top_score)

    if len(top_candidates) > 1:
        candidates = tuple(
            CandidateMatch(
                job_id=job.job_id, score=score, evidence=tuple(evidence[:MATCH_EVIDENCE_MAX_ITEMS])
            )
            for job, score, evidence in sorted(top_candidates, key=lambda entry: entry[0].job_id)[
                :MATCH_CANDIDATE_LIST_MAX_ITEMS
            ]
        )
        return EmailMatchResult(
            match_type="AMBIGUOUS",
            matched_job_id=None,
            confidence=confidence,
            score=top_score,
            evidence=(),
            candidates=candidates,
        )

    job, score, evidence = top_candidates[0]
    return EmailMatchResult(
        match_type=_match_type_for_status(job.status),
        matched_job_id=job.job_id,
        confidence=confidence,
        score=score,
        evidence=tuple(evidence[:MATCH_EVIDENCE_MAX_ITEMS]),
        candidates=(),
    )
