"""Deterministic evidence-based matching of one inbound email to a
tracked `JobRecord` (Stage 7B).

**Codex remediation round 1 (7B-001/002/006/007/008, ambiguity margin).**
This module was reworked after an independent review reproduced concrete
defeats of the intended precedence contract. See each function's
docstring for the specific fix; the high-level architecture is now:

**Job vs. Application.** This project has no separate `ApplicationRecord`
— `JobRecord` (app.db.models.JobRecord) carries both the job posting AND
its `status` (`ApplicationStatus`). "Matched to an application" here
means matched to a `JobRecord` whose `status` indicates the candidate has
actually acted on it (APPLIED/INTERVIEW/REJECTED/OFFER/WITHDRAWN, see
`_APPLICATION_STATUSES`); "matched to a job only" means matched to a
`JobRecord` still at NEW/SAVED. See `_match_type_for_status`.

**No arbitrary winner (spec: "Do not match by guessing").** Every
candidate `JobRecord` is scored independently and deterministically; if
more than one candidate is within `AMBIGUITY_SCORE_MARGIN` points of the
top score, the result is `AMBIGUOUS` with every near-tied candidate
returned — never a coin-flip (or single-noisy-token) pick among
near-equals.

**Precedence — three ordered, hard-stopping tiers, not weight-table
comparison (7B-001 fix).** Composite score-table comparison alone could
not GUARANTEE that an explicit reference always outranks composite
evidence (a Codex review reproduced 93 > 90). Precedence is now
structural:

1. **`_resolve_by_thread_association`** — if a single distinct job is
   associated with prior (non-ambiguous) analyses in the same trusted
   `GmailThreadRecord` thread, that decides `matched_job_id` outright;
   nothing else runs. "Trusted" relies on Stage 7A's own
   thread-membership guarantee (GMAIL-011) for GROUPING, but grouping
   alone is no longer sufficient for HIGH confidence — see 7B-006's fix,
   `_is_thread_corroborated`: an uncorroborated association (no
   continuity signal in THIS message — company/domain/title/reference)
   still names the associated job (useful information) but at `LOW`
   confidence, which forces human review via
   `determine_requires_human_review`'s existing LOW-confidence rule.
   Composite scoring for other candidates never runs once this tier
   fires.
2. **`_resolve_by_explicit_reference`** — if the email's own extracted
   reference tokens (see `extract_reference_tokens`) match exactly one
   candidate's reference tokens, that candidate wins outright at `HIGH`
   — a real precedence rule, not a weight comparison that composite
   evidence could ever mathematically exceed. Matching more than one
   candidate returns `AMBIGUOUS` (never an arbitrary pick). Matching
   zero candidates does NOT fabricate a winner — it falls through to
   composite scoring, carrying a `JOB_REFERENCE_UNRESOLVED` evidence
   note (weight 0) so the response honestly records that an explicit
   reference was present but didn't resolve.
3. **Composite scoring** (`_score_candidate` + `AMBIGUITY_SCORE_MARGIN`)
   — company/domain/title/location evidence, only reached when neither
   tier above decided anything.

**Weak/generic evidence never reaches HIGH or MEDIUM alone** — every
generic title token (`_GENERIC_TITLE_WORDS`) contributes at most
`GENERIC_TITLE_TOKEN_WEIGHT` each, capped at
`GENERIC_TITLE_TOKEN_MAX_TOKENS` tokens, so the maximum possible
generic-only score stays below `MATCH_CONFIDENCE_MEDIUM_THRESHOLD` (see
the assertion below). Multiple candidates that tie (or nearly tie, within
`AMBIGUITY_SCORE_MARGIN`) ONLY on generic overlap (e.g. five different
"Python Developer" applications) still correctly resolve to `AMBIGUOUS`,
not to an arbitrarily "first" one — the spec's own worked example.

**Bounded, always.** The candidate `JobRecord` pool this module scores is
supplied by the caller already bounded (see
`MATCH_CANDIDATE_SCAN_LIMIT`/`REFERENCE_TARGETED_SCAN_LIMIT` and
app/db/gmail_analysis_repository.py's `get_job_candidates`, 7B-007's
fix for why a recency-only bound could hide an exact reference match) —
this module never queries a database itself and has no way to cause an
unbounded scan on its own. Every evidence VALUE is truncated to
`EVIDENCE_FRAGMENT_MAX_LENGTH` AFTER it is fully constructed (7B-008
fix — truncating only some sources, or truncating inputs before joining
multiple tokens together, left a gap where a pathological title could
still produce an oversized persisted fragment).
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
# 7B-007: a SECOND, small, targeted query for candidates whose OWN
# reference tokens match the email's — merged with the recency-bounded
# pool so an old-but-exactly-referenced job can never be hidden purely
# by recency ranking (a fixed LIMIT 500 recency scan alone cannot
# guarantee this — see get_job_candidates).
REFERENCE_TARGETED_SCAN_LIMIT = 50

MATCH_EVIDENCE_MAX_ITEMS = 10
MATCH_CANDIDATE_LIST_MAX_ITEMS = 5
EVIDENCE_FRAGMENT_MAX_LENGTH = 120
# 7B-002/007: bounds how many distinct reference tokens one email/job can
# contribute — a large adversarial body with thousands of labelled
# "Referenz: X" occurrences must not blow up the targeted DB query's OR
# clause or the evidence payload. Deterministic (sorted) truncation.
MAX_REFERENCE_TOKENS = 10

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

# Ambiguity margin (Codex review): exact-tie-only ambiguity detection let
# a single noisy token (e.g. one extra generic word) manufacture a false
# winner between two near-equal composite candidates (41 vs 40, 41 vs
# 39). Candidates within this many points of the top COMPOSITE score are
# now treated as tied. Deliberately smaller than COMPANY_EXACT (35) so a
# genuine single strong signal still decides outright; deliberately
# larger than one title token's weight (6) or LOCATION_OVERLAP (8) so
# that kind of single-signal noise can no longer manufacture a winner.
# Only applies to the composite-scoring tier — the two decisive tiers
# above (thread association, explicit reference) short-circuit before
# this margin is ever consulted.
AMBIGUITY_SCORE_MARGIN = 10

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
# Path-segment ID pattern — alphanumeric (not numeric-only: real-world
# ATS reference codes like "ABC123"/"REF555" mix letters and digits), but
# a segment is only ever kept as a reference token if it contains AT
# LEAST ONE DIGIT (filtered in _extract_url_ids) — this excludes generic
# path words ("roles", "jobs", "careers") from ever being treated as a
# reference on their own, while still accepting "ABC123".
_URL_ID_PATTERN = re.compile(r"/([A-Za-z0-9][A-Za-z0-9\-]{2,19})(?=[/?]|$)")
# 7B-002: a URL-SHAPED substring (domain-with-dot-TLD, optional path) —
# path-segment IDs are only ever extracted from WITHIN a match of this
# pattern, never from bare numbers/codes floating in prose. This is what
# keeps a year ("2026"), a postal code ("60311"), or a phone number out
# of reference-token consideration: none of those strings contain a
# `something.tld`-shaped domain, so none of them can match this pattern
# at all, regardless of digit count.
_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[\w.\-/?=&%]*)?", re.IGNORECASE
)


def _extract_url_ids(url_text: str) -> set[str]:
    return {
        match.group(1).upper()
        for match in _URL_ID_PATTERN.finditer(url_text)
        if any(char.isdigit() for char in match.group(1))
    }


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
    this module trusts `thread_id` grouping for GROUPING, and
    `_is_thread_corroborated` for why grouping alone is no longer
    sufficient for HIGH confidence (7B-006).
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
    """Bounds an evidence VALUE to `EVIDENCE_FRAGMENT_MAX_LENGTH`. Must be
    the LAST step applied to a value before it goes into a
    `MatchEvidenceItem` (7B-008) — calling this on an input before
    joining/combining it with other data does not bound the combined
    result.
    """
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


def _extract_urls(text: str) -> list[str]:
    return _URL_PATTERN.findall(text)[:MAX_REFERENCE_TOKENS]


def extract_reference_tokens(text: str, url: str = "") -> frozenset[str]:
    """Bounded, best-effort extraction of explicit job/application
    reference identifiers (spec: "explicit job reference / vacancy ID").

    Two independent sources, both bounded to `MAX_REFERENCE_TOKENS` total
    tokens (deterministic — sorted before truncation):

    1. An explicit LABELLED reference in `text` ("Referenz-Nr: ABC-123",
       "Job ID: 4821", "Kennziffer XYZ").
    2. An alphanumeric path segment (must contain at least one digit —
       see `_extract_url_ids`) found INSIDE a URL-shaped substring —
       either `url` itself (a JobCandidate's own URL) or any URL-shaped
       substring appearing in `text` (7B-002 fix: an email that pastes
       the job's own URL, or a bounded textual variant of it, is now
       recognized, including alphanumeric ATS reference codes like
       "ABC123", not only pure numbers). Critically, path-segment IDs are
       NEVER extracted from bare numbers/codes in prose — see
       `_URL_PATTERN`'s docstring note: a year, a postal code, or a phone
       number cannot match a domain-shaped pattern, so none of those can
       become a reference token via this path.

    Matching is exact-token, never fuzzy — an accidental substring
    collision must not fabricate evidence.
    """
    tokens = {match.group(1).upper() for match in _REFERENCE_PATTERN.finditer(text)}
    for found_url in _extract_urls(text):
        tokens.update(_extract_url_ids(found_url))
    if url:
        tokens.update(_extract_url_ids(url))
    return frozenset(sorted(tokens)[:MAX_REFERENCE_TOKENS])


def _is_thread_corroborated(
    job: JobCandidate,
    *,
    email_text: str,
    email_tokens: set[str],
    sender_domain: str | None,
    email_reference_tokens: frozenset[str],
) -> bool:
    """7B-006: plain Stage-7A thread MEMBERSHIP is a transport/threading
    heuristic, not proof that a new sender belongs to the same
    recruitment conversation — an attacker/unrelated sender can set
    `References` to a legitimate root and land in the same
    `GmailThreadRecord` (Stage 7A groups by header value, not by sender
    identity/authentication; RFC 5322 Message-ID is untrusted correlation
    evidence, never cryptographically authenticated). A thread
    association is only treated as strong (HIGH) when at least one
    INDEPENDENT continuity signal is present in the CURRENT message:
    normalized company name, sender domain matching the job's own domain
    (excluding free-mail), a distinctive (non-generic) title token, or an
    explicit reference match. Absent all four, the association is
    demoted to LOW confidence by the caller — still informative, but
    forcing human review rather than silently inheriting HIGH trust.
    """
    normalized_company = normalize_company_name(job.company)
    normalized_email_text = normalize_company_name(email_text)
    if len(normalized_company) >= 3 and f" {normalized_company} " in f" {normalized_email_text} ":
        return True

    job_domain = extract_company_domain(job.url)
    if (
        sender_domain
        and job_domain
        and sender_domain not in FREE_MAIL_DOMAINS
        and sender_domain == job_domain
    ):
        return True

    distinctive_job_tokens = {
        token for token in _title_tokens(job.title) if token not in _GENERIC_TITLE_WORDS
    }
    if distinctive_job_tokens & email_tokens:
        return True

    job_reference_tokens = extract_reference_tokens(job.title, job.url)
    if job_reference_tokens & email_reference_tokens:
        return True

    return False


def _resolve_by_thread_association(
    thread_prior_matches: list[ThreadPriorMatch],
    job_candidates: list[JobCandidate],
    *,
    email_text: str,
    email_tokens: set[str],
    sender_domain: str | None,
    email_reference_tokens: frozenset[str],
) -> EmailMatchResult | None:
    distinct_thread_jobs = {m.job_id for m in thread_prior_matches}
    if len(distinct_thread_jobs) != 1:
        return None

    job_id = next(iter(distinct_thread_jobs))
    candidate = next((c for c in job_candidates if c.job_id == job_id), None)

    if candidate is not None:
        match_type = _match_type_for_status(candidate.status)
        corroborated = _is_thread_corroborated(
            candidate,
            email_text=email_text,
            email_tokens=email_tokens,
            sender_domain=sender_domain,
            email_reference_tokens=email_reference_tokens,
        )
    else:
        # The associated job fell outside the bounded candidate scan (or
        # no longer exists) — fall back to the trusted prior analysis's
        # own recorded match_type rather than guessing, and treat as
        # uncorroborated (nothing to corroborate against).
        match_type = thread_prior_matches[0].match_type
        corroborated = False

    weight = MATCH_EVIDENCE_WEIGHTS["THREAD_ASSOCIATION"]
    kind = "THREAD_ASSOCIATION" if corroborated else "THREAD_ASSOCIATION_UNCORROBORATED"
    confidence: Confidence = "HIGH" if corroborated else "LOW"
    return EmailMatchResult(
        match_type=match_type,
        matched_job_id=job_id,
        confidence=confidence,
        score=weight,
        evidence=(MatchEvidenceItem(kind, _truncate(str(job_id)), weight),),
        candidates=(),
    )


def _resolve_by_explicit_reference(
    job_candidates: list[JobCandidate], email_reference_tokens: frozenset[str]
) -> tuple[EmailMatchResult | None, tuple[MatchEvidenceItem, ...]]:
    """7B-001: a true precedence rule, not a weight comparison composite
    evidence could mathematically ever exceed. Returns
    `(decisive_result_or_None, carry_forward_evidence)` — the second
    element is only ever non-empty when reference tokens were present in
    the email but resolved to zero candidates (an honest
    `JOB_REFERENCE_UNRESOLVED` note, weight 0, carried into whatever
    composite result follows — never fabricating a winner from an
    unresolved reference).
    """
    if not email_reference_tokens:
        return None, ()

    matches: list[tuple[JobCandidate, frozenset[str]]] = []
    for job in job_candidates:
        job_reference_tokens = extract_reference_tokens(job.title, job.url)
        overlap = job_reference_tokens & email_reference_tokens
        if overlap:
            matches.append((job, overlap))

    if not matches:
        unresolved = ",".join(sorted(email_reference_tokens))
        return None, (MatchEvidenceItem("JOB_REFERENCE_UNRESOLVED", _truncate(unresolved), 0),)

    weight = MATCH_EVIDENCE_WEIGHTS["JOB_REFERENCE"]
    distinct_jobs = {job.job_id for job, _overlap in matches}

    if len(distinct_jobs) > 1:
        candidates = tuple(
            CandidateMatch(
                job_id=job.job_id,
                score=weight,
                evidence=(
                    MatchEvidenceItem("JOB_REFERENCE", _truncate(sorted(overlap)[0]), weight),
                ),
            )
            for job, overlap in sorted(matches, key=lambda entry: entry[0].job_id)[
                :MATCH_CANDIDATE_LIST_MAX_ITEMS
            ]
        )
        return (
            EmailMatchResult(
                match_type="AMBIGUOUS",
                matched_job_id=None,
                confidence="HIGH",
                score=weight,
                evidence=(),
                candidates=candidates,
            ),
            (),
        )

    job, overlap = matches[0]
    return (
        EmailMatchResult(
            match_type=_match_type_for_status(job.status),
            matched_job_id=job.job_id,
            confidence="HIGH",
            score=weight,
            evidence=(MatchEvidenceItem("JOB_REFERENCE", _truncate(sorted(overlap)[0]), weight),),
            candidates=(),
        ),
        (),
    )


def _score_candidate(
    job: JobCandidate,
    *,
    normalized_email_text: str,
    email_text: str,
    email_tokens: set[str],
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
        evidence.append(MatchEvidenceItem("DOMAIN_COMPANY_MATCH", _truncate(sender_domain), weight))

    job_tokens = _title_tokens(job.title)
    distinctive = sorted(
        t for t in job_tokens if t not in _GENERIC_TITLE_WORDS and t in email_tokens
    )
    generic = sorted(t for t in job_tokens if t in _GENERIC_TITLE_WORDS and t in email_tokens)
    if distinctive:
        counted = distinctive[:TITLE_TOKEN_MAX_TOKENS]
        weight = len(counted) * TITLE_TOKEN_WEIGHT
        score += weight
        # 7B-008: truncate the FINAL joined value — bounding each token
        # individually before joining would not bound a pathological
        # single very-long token, and would not bound the joined result
        # either.
        evidence.append(
            MatchEvidenceItem("TITLE_TOKEN_OVERLAP", _truncate(",".join(counted)), weight)
        )
    if generic:
        counted = generic[:GENERIC_TITLE_TOKEN_MAX_TOKENS]
        weight = len(counted) * GENERIC_TITLE_TOKEN_WEIGHT
        score += weight
        evidence.append(
            MatchEvidenceItem("GENERIC_TITLE_TOKEN_OVERLAP", _truncate(",".join(counted)), weight)
        )

    location = job.location.strip()
    if location and location.casefold() in email_text.casefold():
        weight = MATCH_EVIDENCE_WEIGHTS["LOCATION_OVERLAP"]
        score += weight
        evidence.append(MatchEvidenceItem("LOCATION_OVERLAP", _truncate(location), weight))

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
    result naming every near-tied candidate, or UNMATCHED — never an
    arbitrary pick among near-equal candidates.
    """
    email_text = f"{subject}\n{body_plain}"
    normalized_email_text = normalize_company_name(email_text)
    email_tokens = _title_tokens(email_text)
    email_reference_tokens = extract_reference_tokens(email_text)
    sender_domain = extract_sender_domain(from_address)

    thread_result = _resolve_by_thread_association(
        thread_prior_matches,
        job_candidates,
        email_text=email_text,
        email_tokens=email_tokens,
        sender_domain=sender_domain,
        email_reference_tokens=email_reference_tokens,
    )
    if thread_result is not None:
        return thread_result

    reference_result, carry_forward_evidence = _resolve_by_explicit_reference(
        job_candidates, email_reference_tokens
    )
    if reference_result is not None:
        return reference_result

    scored = [
        (
            job,
            *_score_candidate(
                job,
                normalized_email_text=normalized_email_text,
                email_text=email_text,
                email_tokens=email_tokens,
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
            evidence=carry_forward_evidence,
            candidates=(),
        )

    top_score = max(score for _, score, _ in scored)
    # 7B: ambiguity margin, not exact-tie-only — see AMBIGUITY_SCORE_MARGIN.
    near_top = [entry for entry in scored if entry[1] >= top_score - AMBIGUITY_SCORE_MARGIN]
    confidence = _confidence_for_score(top_score)

    if len(near_top) > 1:
        candidates = tuple(
            CandidateMatch(
                job_id=job.job_id, score=score, evidence=tuple(evidence[:MATCH_EVIDENCE_MAX_ITEMS])
            )
            for job, score, evidence in sorted(
                near_top, key=lambda entry: (-entry[1], entry[0].job_id)
            )[:MATCH_CANDIDATE_LIST_MAX_ITEMS]
        )
        return EmailMatchResult(
            match_type="AMBIGUOUS",
            matched_job_id=None,
            confidence=confidence,
            score=top_score,
            evidence=carry_forward_evidence,
            candidates=candidates,
        )

    job, score, evidence = near_top[0]
    combined_evidence = (tuple(evidence) + carry_forward_evidence)[:MATCH_EVIDENCE_MAX_ITEMS]
    return EmailMatchResult(
        match_type=_match_type_for_status(job.status),
        matched_job_id=job.job_id,
        confidence=confidence,
        score=score,
        evidence=combined_evidence,
        candidates=(),
    )
