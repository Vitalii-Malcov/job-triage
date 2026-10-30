"""Stage 17C: independent fit criteria for one vacancy -- no overall score.

Each criterion has its own status, a short reason and (where the posting
states it) evidence. A critical constraint can therefore never be hidden by a
good result elsewhere:

- skills / experience come from the persisted matcher v12 analysis (the
  matcher itself is unchanged; NOT_EVALUATED if the job was never matched);
- language compares the posting's German requirement with the candidate's
  confirmed level (a mandatory C1 with a confirmed B1 is a MAJOR_GAP);
- location / commute is PASS or FAIL only from a stored car-route check
  (`job_commute_checks`). Without one, Stage 22 P1 estimates from the BA
  coordinates (ESTIMATE_WITHIN / ESTIMATE_NEAR_LIMIT / ESTIMATE_BEYOND, or
  LOCATION_UNVERIFIED when the workplace is doubtful); without coordinates a
  known workplace is DISTANCE_UNVERIFIED, an unknown one UNKNOWN. An
  estimate ranks the vacancy (tier) but is never the car route: the
  decision stays NEEDS_VERIFICATION until a route is checked;
- remote compatibility needs a confirmed fully-remote posting that does not
  exclude the candidate's region ("bis zu 100%" is not confirmed); the BA's
  own home-office flag only fills in a posting text that says nothing
  (Stage 22 P1);
- salary, entry level and start date are informational, never blocking;
- duplicate risk reuses the Stage 15 tracker check (ALREADY_APPLIED blocks).

`decision` is BLOCKED (a hard constraint fails), NEEDS_VERIFICATION
(something must be checked by hand first) or CANDIDATE. It is a triage
label, not a ranking, and never an instruction to apply.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from app.agents.candidate_job_matcher import ALGORITHM_VERSION as MATCHER_VERSION
from app.agents.geo_estimate import commutable_state, estimate_commute, state_key
from app.agents.job_fit_signals import (
    asks_immediate_start,
    classify_remote,
    entry_level_signals,
    extract_german_requirement,
    extract_salary,
)
from app.agents.role_relevance_classifier import classify_title_relevance
from app.agents.seniority_classifier import classify_title_seniority
from app.agents.workplace_address import (
    WorkplaceAddress,
    address_words,
    best_workplace,
    city_key,
    city_qualifiers,
    extract_addresses,
    stated_workplace_towns,
)
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    get_cached_match,
    get_latest_match,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import get_candidate_profile
from app.db.candidate_search_repository import load_search_config
from app.db.job_commute_repository import is_approximate, latest_commute_check
from app.db.models import JobCommuteCheckRecord, JobRecord
from app.models.candidate_job_match import CandidateJobMatch
from app.models.candidate_profile import is_usable_for_generation, normalize_text_identity
from app.models.candidate_search import CandidateSearchSettings
from app.services.application_duplicates import is_intermediary, verdict
from app.services.application_guard import sent_duplicate, vacancy_findings

Decision = Literal["BLOCKED", "NEEDS_VERIFICATION", "CANDIDATE"]
# Stage 19: a triage tier on top of `decision` (which stays the Stage 17
# contract). `decision` blocks only on hard facts (route, remote region,
# already applied); the tier also weighs language, experience, skills, role
# and salary so an operator sees which vacancies are worth the time. It
# never blocks anything and is never an instruction to apply.
# Stage 22 P1 adds STRETCH ("worth trying despite a stated gap"). The names
# map to the operator vocabulary: NOT_SUITABLE = unsuitable, NEEDS_OPERATOR =
# needs verification (data missing), STRETCH, PARTIAL_MATCH, GOOD_MATCH.
FitTier = Literal["NOT_SUITABLE", "NEEDS_OPERATOR", "STRETCH", "PARTIAL_MATCH", "GOOD_MATCH"]

_LEVEL = {"UNKNOWN": 0, "A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}

# Statuses that make the whole vacancy BLOCKED / NEEDS_VERIFICATION.
_BLOCKING = {
    ("location", "FAIL"),
    ("remote", "INCOMPATIBLE"),
    ("duplicate", "ALREADY_APPLIED"),
}
# Night audit 2026-10-27: an approximate route (city centre / postcode)
# decides only with this margin; an exact route this close to the limit is
# flagged in the reason.
APPROXIMATE_MARGIN_KM = 15
NEAR_LIMIT_KM = 5

# Stage 22 P1: verifications that still allow a GOOD/PARTIAL tier. The tier is
# a preliminary ranking (A); the car route stays a pre-Bewerbung check (B):
# `decision` keeps every one of them NEEDS_VERIFICATION.
_TIER_ACCEPTS = {
    ("location", "ESTIMATE_WITHIN"),
    ("location", "PASS_APPROXIMATE"),
}
# Stage 22 P1: a straight line beyond the car-route limit is a lower bound
# of the route -- the vacancy is out of reach (its workplace was not doubted).
_TIER_BARRIERS = {
    ("location", "ESTIMATE_BEYOND"),
    # Stage 22 P1: an explicitly unrelated profession or a senior title is
    # never a fit for the junior target, however many skills match.
    ("role", "OFF_ROLE"),
    ("entry_level", "SENIOR_TITLE"),
    ("posting_type", "SELF_EMPLOYMENT"),
    ("posting_type", "APPRENTICESHIP"),
}
# Stage 22 P1: a stated gap the candidate may still try (STRETCH).
_STRETCH = {
    ("language", "MAJOR_GAP"),
    ("language", "GAP"),
    ("language", "GAP_TO_VERIFY"),
    ("experience", "GAP"),
    ("entry_level", "EXPERIENCED"),
    ("education", "IT_DEGREE_REQUIRED"),
    ("entry_level", "STUDENT_JOB"),
    ("skills", "WEAK"),
}
_WEAKER = {
    ("skills", "PARTIAL"),
    ("role", "OTHER"),
    ("experience", "PARTIAL"),
    ("language", "MINOR_GAP"),
    ("salary", "BELOW"),
    ("entry_level", "MIXED"),
}

_VERIFY = {
    ("location", "DISTANCE_UNVERIFIED"),
    ("location", "APPROXIMATE_NEAR_LIMIT"),
    # Codex review: a workplace can lie further than the margin from the
    # city centre; an approximate PASS is never enough on its own.
    ("location", "PASS_APPROXIMATE"),
    ("location", "UNKNOWN"),
    ("location", "NOT_CONFIGURED"),
    # Stage 22 P1: a coordinate estimate is never the car route -- the
    # decision (pre-Bewerbung check) stays NEEDS_VERIFICATION for all of them.
    ("location", "ESTIMATE_WITHIN"),
    ("location", "ESTIMATE_NEAR_LIMIT"),
    ("location", "ESTIMATE_BEYOND"),
    ("location", "ESTIMATE_BEYOND_RELOCATION"),
    ("location", "LOCATION_UNVERIFIED"),
    ("duplicate", "POSSIBLE_DUPLICATE"),
    ("skills", "NOT_EVALUATED"),
}


@dataclass(frozen=True)
class Criterion:
    name: str
    status: str
    reason: str
    evidence: str | None = None


@dataclass
class FitAssessment:
    job_id: int
    company: str
    title: str
    criteria: list[Criterion] = field(default_factory=list)
    # Facts the tier needs beyond the criteria's status (Codex Stage 19 plan
    # review): a language gap counts as a barrier only when the posting states
    # the level explicitly and the candidate's level is actually confirmed.
    german_requirement_basis: str | None = None
    german_level_confirmed: bool = False
    # Codex Stage 19 review rounds 1-4 (rule redesigned): an experience gap
    # is a barrier only with positive evidence -- a CURRENT matcher analysis
    # that lists a stated experience requirement as MISSING from the
    # profile. "N Jahre" wording alone, UNKNOWN, a confirmed match or an
    # out-of-date analysis go to the operator instead.
    experience_missing_in_profile: bool = False
    # Stage 22 P1: "verhandlungssicher" / "fließend" / "Muttersprache" (or a
    # CEFR C level) stated as mandatory is as clear as an explicit level.
    german_requirement_strong: bool = False
    # The largest "N Jahre Erfahrung" the posting states.
    required_years: int | None = None

    def criterion(self, name: str) -> Criterion:
        return next(c for c in self.criteria if c.name == name)

    @property
    def blockers(self) -> list[Criterion]:
        return [c for c in self.criteria if (c.name, c.status) in _BLOCKING]

    @property
    def verifications(self) -> list[Criterion]:
        return [c for c in self.criteria if (c.name, c.status) in _VERIFY]

    @property
    def decision(self) -> Decision:
        if self.blockers:
            return "BLOCKED"
        if self.verifications:
            return "NEEDS_VERIFICATION"
        return "CANDIDATE"

    @property
    def fit_tier(self) -> FitTier:
        return self._tier()[0]

    @property
    def tier_reasons(self) -> list[str]:
        return self._tier()[1]

    def _tier(self) -> tuple[FitTier, list[str]]:
        """Stage 22 P1 order: NOT_SUITABLE (a barrier) > NEEDS_OPERATOR (data
        missing / to verify) > STRETCH (worth trying despite a stated gap) >
        PARTIAL_MATCH > GOOD_MATCH. A tier never blocks anything."""
        by_name = {c.name: c for c in self.criteria}

        def why(c: Criterion) -> str:
            evidence = f' (evidence: "{c.evidence}")' if c.evidence else ""
            return f"{c.name} {c.status}: {c.reason}{evidence}"

        language = by_name.get("language")
        experience = by_name.get("experience")
        entry = by_name.get("entry_level")
        barriers = [why(c) for c in self.blockers]
        barriers += [why(c) for c in self.criteria if (c.name, c.status) in _TIER_BARRIERS]
        language_barrier = (
            language is not None
            and language.status == "MAJOR_GAP"
            and self.german_level_confirmed
            and (
                self.german_requirement_basis == "EXPLICIT_REQUIREMENT"
                or self.german_requirement_strong
            )
        )
        if language_barrier:
            barriers.append(why(language))
        experience_barrier = (
            experience is not None
            and experience.status == "GAP"
            and self.experience_missing_in_profile
            and (
                (entry is not None and entry.status == "EXPERIENCED")
                or (self.required_years or 0) >= 3
            )
        )
        if experience_barrier:
            barriers.append(why(experience))
        if barriers:
            return "NOT_SUITABLE", barriers
        checks = [why(c) for c in self.verifications if (c.name, c.status) not in _TIER_ACCEPTS]
        # Stage 22 P1 (Codex): a title outside the IT vocabulary is a question
        # for the operator, never proof of an unrelated role.
        checks += [why(c) for c in self.criteria if (c.name, c.status) == ("role", "NO_IT_SIGNAL")]
        if checks:
            return "NEEDS_OPERATOR", checks
        stretch = [why(c) for c in self.criteria if (c.name, c.status) in _STRETCH]
        if stretch:
            return "STRETCH", stretch
        weaker = [why(c) for c in self.criteria if (c.name, c.status) in _WEAKER]
        skills = by_name.get("skills")
        if weaker or skills is None or skills.status != "STRONG":
            return "PARTIAL_MATCH", weaker or ([why(skills)] if skills else [])
        strengths = [
            why(c)
            for c in self.criteria
            if c.name in {"skills", "role", "language", "location", "entry_level", "education"}
        ]
        return "GOOD_MATCH", strengths


# --- individual criteria (pure) ------------------------------------------------


def skills_criterion(match: CandidateJobMatch | None) -> Criterion:
    if match is None:
        return Criterion("skills", "NOT_EVALUATED", "no matcher analysis stored for this job")
    missing = [
        r.requirement
        for r in match.missing_requirements
        if r.requirement_type == "SKILL" and r.importance == "REQUIRED"
    ]
    score = match.required_skill_score
    if score >= 70 and not missing:
        status = "STRONG"
    elif score >= 40:
        status = "PARTIAL"
    else:
        status = "WEAK"
    reason = f"required skill score {score}/100 ({match.algorithm_version})"
    if missing:
        reason += "; missing required: " + ", ".join(missing[:6])
    return Criterion("skills", status, reason)


_WORD = re.compile(r"[a-z0-9äöüß+#]+")
_ROLE_FILLER = frozenset({"junior", "trainee", "m", "w", "d", "und", "and", "mit", "with"})


def _role_words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold())) - _ROLE_FILLER


# Stage 22 P1: never a fit for the candidate's target (PhD research, teaching).
_OFF_ROLE_TITLE = re.compile(
    r"\b(?:doktorand\w*|ph\.?d\b\.?|doctoral|postdoc\w*|promotionsstelle|trainer(?:in)?|dozent\w*"
    r"|lehrkraft|lehrer(?:in)?)\b",
    re.IGNORECASE,
)
# Commercial occupations -- OFF_ROLE only when the title names no IT word at
# all ("IT-Support für Vertriebssoftware" stays a technical role).
_COMMERCIAL_TITLE = re.compile(
    r"\b(?:sales|vertrieb\w*|account\s+manager|office\s+manager|sekret(?:ä|ae)r\w*|secretar\w*"
    r"|buchhalt\w*|kaufm(?:ann|frau|ännisch|aennisch)\w*)\b",
    re.IGNORECASE,
)
# Stage 22 P1: a title must name SOME IT / software / data / technical role.
_IT_TITLE_WORDS = re.compile(
    r"entwickl|develop|programm|software|informati|\bit\b|\bit-|devops|engineer|ingenieur|techni"
    r"|daten|\bdata|analyst|admin|system|netzwerk|network|cloud|\bsap\b|\berp\b|web|\bapp|digital"
    r"|automatis|automati[oz]|test|\bqa\b|qualit(?:ä|ae)tssicherung|support|helpdesk|\bcode|python|java"
    r"|\bsql\b|backend|frontend|full.?stack|\bki\b|\bai\b|machine|security|sicherheit|edv|\bsps\b|mechatron"
    r"|service\s*desk|helpdesk|hotline|level\s*[12]|[12](?:st|nd)\s*level|anwendungsbetreu"
    r"|anwender|computer|rechenzentrum|elektroni",
    re.IGNORECASE,
)
# Stage 11B lists them as "not software development", but technical IT
# support / administration is one of the candidate's alternative targets.
_IT_ADJACENT_SIGNALS = frozenset({"administrator", "systemadministrator"})


def role_criterion(title: str, settings: CandidateSearchSettings) -> Criterion:
    """Which configured role priority the title matches (informational: it
    never blocks, and no "Junior" wording is required). A role matches when
    every significant word of one of its "/"-alternatives is in the title."""
    academic = _OFF_ROLE_TITLE.search(title)
    if academic is not None:
        # Stage 22 P1: before role priorities -- "Dozent Python Developer" is
        # a teaching job even when "Python Developer" is configured.
        return Criterion("role", "OFF_ROLE", f"title names a non-IT position ({academic.group(0)})")
    if not settings.primary_roles and not settings.secondary_roles:
        return Criterion("role", "NOT_CONFIGURED", "no role priorities configured")
    title_words = set(_WORD.findall(title.casefold()))
    for tier, roles in (
        ("PRIMARY", settings.primary_roles),
        ("SECONDARY", settings.secondary_roles),
    ):
        for role in roles:
            for alternative in role.split("/"):
                words = _role_words(alternative)
                if words and words <= title_words:
                    return Criterion("role", tier, f"title matches '{role}'")
    other_profession = (
        _COMMERCIAL_TITLE.search(title) if not _IT_TITLE_WORDS.search(title) else None
    )
    if other_profession is not None:
        return Criterion(
            "role",
            "OFF_ROLE",
            f"title names a non-IT position ({other_profession.group(0)})",
        )
    relevance = classify_title_relevance(title)
    if relevance.level == "IRRELEVANT" and relevance.matched_signal not in _IT_ADJACENT_SIGNALS:
        # Stage 22 P1: an explicitly non-software profession ("Aktuar",
        # "Elektroingenieur"), never only "no configured role matched".
        return Criterion(
            "role",
            "OFF_ROLE",
            f"title names another profession ({relevance.matched_signal}); "
            "matching skills do not make it a software role",
        )
    if not _IT_TITLE_WORDS.search(title):
        # Stage 22 P1: no IT, software, data or technical word at all ("Office
        # Manager", "Account Manager"): keyword overlap in the text is noise.
        return Criterion("role", "NO_IT_SIGNAL", "title names no IT/technical role")
    return Criterion("role", "OTHER", "title matches no configured role")


def experience_criterion(match: CandidateJobMatch | None, title: str, text: str) -> Criterion:
    signals = entry_level_signals(title, text)
    statuses = []
    if match is not None:
        statuses = [
            r.match_status
            for r in [*match.matched_requirements, *match.partial_requirements]
            + [*match.missing_requirements, *match.unknown_requirements]
            if r.requirement_type == "EXPERIENCE"
        ]
    if "MISSING" in statuses or (signals.required_years or 0) >= 3:
        years = f" ({signals.required_years}+ years asked)" if signals.required_years else ""
        return Criterion("experience", "GAP", f"stated experience requirement not met{years}")
    if "PARTIAL" in statuses or signals.required_years == 2:
        return Criterion("experience", "PARTIAL", "experience requirement partly supported")
    if statuses and all(s == "MATCH" for s in statuses):
        return Criterion("experience", "OK", "experience requirements supported by the profile")
    if signals.required_years is not None and signals.required_years <= 1:
        return Criterion("experience", "OK", f"{signals.required_years} year(s) asked")
    return Criterion("experience", "UNKNOWN", "no explicit experience requirement found")


def language_criterion(text: str, german_level: str) -> Criterion:
    requirement = extract_german_requirement(text)
    if requirement.level is None:
        return Criterion("language", "NO_REQUIREMENT", "no German level stated")
    asked, held = _LEVEL[requirement.level], _LEVEL.get(german_level, 0)
    if requirement.basis == "INFERRED_REQUIREMENT":
        label = (
            f"German ~{requirement.level} required (inferred from wording, no CEFR level stated)"
        )
    else:
        label = f"German {requirement.level} {'required' if requirement.mandatory else 'preferred'}"
    reason = f"{label}; confirmed level {german_level}"
    if held >= asked:
        return Criterion("language", "OK", reason, requirement.evidence)
    if not requirement.mandatory:
        return Criterion("language", "MINOR_GAP", reason, requirement.evidence)
    if requirement.basis == "INFERRED_REQUIREMENT":
        if asked - held >= 2 and _strong_wording(requirement):
            # Stage 22 P1: mandatory "verhandlungssicher" / "fließend" /
            # native is as clear as a stated C1 (the tier treats it so).
            return Criterion(
                "language", "MAJOR_GAP", reason + " (fluency stated)", requirement.evidence
            )
        # "sehr gute Deutschkenntnisse" is not a C1 certificate: a gap to
        # verify (ask / read the whole posting), never a stated MAJOR_GAP.
        return Criterion("language", "GAP_TO_VERIFY", reason, requirement.evidence)
    status = "MAJOR_GAP" if asked - held >= 2 else "GAP"
    return Criterion("language", status, reason, requirement.evidence)


def _ba_home_office(job: JobRecord | None) -> Criterion | None:
    """Stage 22 P1: the BA's own home-office flag, used only when the text
    says nothing. Never FULLY_REMOTE: "possible" / "up to N %" is not a
    confirmed fully remote job."""
    if job is None or getattr(job, "home_office_possible", None) is None:
        return None
    if not job.home_office_possible:
        return Criterion("remote", "ONSITE", "BA: no home office", "homeofficemoeglich=false")
    kind = (job.home_office_type or "").replace("_", " ").lower()
    share = f", up to {job.home_office_percent} %" if job.home_office_percent else ""
    detail = f" ({kind}{share})" if kind or share else ""
    return Criterion(
        "remote",
        "PARTIAL_REMOTE",
        f"BA: home office possible{detail}; not confirmed as fully remote",
        "homeofficemoeglich=true",
    )


def remote_criterion(
    text: str, settings: CandidateSearchSettings, job: JobRecord | None = None
) -> Criterion:
    signal = classify_remote(text)
    if signal.mode == "UNKNOWN":
        stated = _ba_home_office(job)
        if stated is not None:
            return stated
    if signal.mode != "FULLY_REMOTE":
        reasons = {
            "PARTIAL_REMOTE": "remote/home office mentioned but not confirmed as fully remote",
            "HYBRID": "hybrid: regular presence at the workplace",
            "ONSITE": "on-site work",
            "UNKNOWN": "no remote information",
        }
        return Criterion("remote", signal.mode, reasons[signal.mode], signal.evidence)
    if settings.remote_work_scope != "GERMANY" or not settings.remote_work_from_region:
        return Criterion("remote", "NOT_CONFIGURED", "remote work rule not configured")
    region = settings.remote_work_from_region
    if signal.region_restriction and signal.region_restriction.casefold() != region.casefold():
        return Criterion(
            "remote",
            "INCOMPATIBLE",
            f"fully remote but only from {signal.region_restriction} "
            f"(candidate works from {region})",
            signal.evidence,
        )
    return Criterion(
        "remote", "COMPATIBLE", f"fully remote; work from {region} allowed", signal.evidence
    )


def _same_origin(check: JobCommuteCheckRecord, settings: CandidateSearchSettings, label) -> bool:
    if check.origin_label != label:
        return False
    current = settings.commute_origin
    if current is None and check.origin_lat is None:
        return True
    if current is None or check.origin_lat is None or check.origin_lon is None:
        return False
    return (round(check.origin_lat, 5), round(check.origin_lon, 5)) == (
        round(current.lat, 5),
        round(current.lon, 5),
    )


def _route_names_address(measured_to: str, posted: WorkplaceAddress) -> bool:
    """True when the stored route names the posted street, its complete
    house number and the postcode (Codex rounds 5-6)."""
    street_and_number = address_words(posted.text.split(",", 1)[0])
    pattern = (
        r"(?<![0-9a-zäöü])"
        + re.escape(street_and_number).replace(r"\ ", r"\s*")
        + r"(?![0-9a-zäöü])"
    )
    target = address_words(measured_to)
    return bool(re.search(pattern, target)) and bool(
        re.search(rf"(?<!\d){posted.postcode}(?!\d)", target)
    )


def _route_elsewhere(label: str, job_location: str | None) -> bool:
    """True when a tool-measured route's destination label does not name
    the posting's city (Codex rounds 11-12: "Frankfurt, Hauptstraße 5" or
    "60311 Frankfurt" for a Hanau posting). A label without any city
    cannot be confirmed either, so it counts as elsewhere."""
    city = city_key(job_location)
    if not city:
        return False
    words = address_words(label)
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        words = words.replace(umlaut, plain)
    present = set(words.split())
    if not all(part in present for part in city.split()):
        return True
    # Same name, other town: "Frankfurt (Oder)" for "Frankfurt am Main".
    wanted, named = city_qualifiers(job_location), city_qualifiers(label)
    return bool(wanted and named and not wanted & named)


def location_criterion(
    job: JobRecord,
    settings: CandidateSearchSettings,
    remote: Criterion,
    check: JobCommuteCheckRecord | None,
) -> Criterion:
    if remote.status == "COMPATIBLE":
        return Criterion("location", "REMOTE_OK", "fully remote and compatible; no commute")
    if settings.max_commute_km is None or settings.commute_measure != "CAR_ROUTE":
        return Criterion("location", "NOT_CONFIGURED", "no car-route commute limit configured")
    limit = settings.max_commute_km
    origin = settings.commute_origin_label or settings.home_city or "home"
    if check is not None and not _same_origin(check, settings, origin):
        return Criterion(
            "location",
            "DISTANCE_UNVERIFIED",
            f"stored car route was measured from another origin ({check.origin_label}); "
            f"re-check from {origin} (limit {limit} km)",
        )
    if check is not None:
        # Codex rounds 4-5: the posting may have changed since the route was
        # measured (same job id, new description). A workplace address now in
        # the posting that the stored route does not name (street, number
        # and postcode) makes the route stale.
        posted = best_workplace(
            job.description or "",
            posted_by_agency=is_intermediary(job.company),
            job_location=job.location,
        )
        # Only the destination label names the point routed to; the note may
        # carry an operator's own wording (Codex round 11).
        if posted is not None and not _route_names_address(check.destination_label, posted):
            return Criterion(
                "location",
                "DISTANCE_UNVERIFIED",
                f"stored route to {check.destination_label} does not match the workplace the "
                f"posting now states ({posted.text}); re-check (limit {limit} km)",
            )
        measured_by_tool = not check.method.startswith("MANUAL")
        if (
            posted is None
            and measured_by_tool
            and _route_elsewhere(check.destination_label, job.location)
        ):
            # No street address in the posting: a route the tool measured
            # must at least end in the posting's city (round 11). A route the
            # operator recorded by hand stays the operator's statement.
            return Criterion(
                "location",
                "DISTANCE_UNVERIFIED",
                f"stored route to {check.destination_label} is not in the posting's location "
                f"({job.location}); re-check (limit {limit} km)",
            )
        minutes = (
            f", ~{check.car_route_minutes:.0f} min free-flow" if check.car_route_minutes else ""
        )
        detail = (
            f"{check.car_route_km:.1f} km by car from {check.origin_label} to "
            f"{check.destination_label}{minutes} ({check.method})"
        )
        relocation = ""
        if check.destination_region and check.destination_region in settings.relocation_regions:
            relocation = f"; relocation within {check.destination_region} would be possible"
        if is_approximate(check.method):
            # Measured to a city centre / postcode / street only: decisive
            # only with a clear margin, never "verified".
            if check.car_route_km > limit + APPROXIMATE_MARGIN_KM:
                return Criterion(
                    "location",
                    "FAIL",
                    f"{detail} > {limit} km even allowing for the approximate "
                    f"destination{relocation}",
                )
            if check.car_route_km < limit - APPROXIMATE_MARGIN_KM:
                return Criterion(
                    "location",
                    "PASS_APPROXIMATE",
                    f"{detail} < {limit} km (approximate destination; not verified)",
                )
            return Criterion(
                "location",
                "APPROXIMATE_NEAR_LIMIT",
                f"{detail}: approximate destination within {APPROXIMATE_MARGIN_KM} km of the "
                f"{limit} km limit; measure to the exact workplace address",
            )
        if check.car_route_km <= limit:
            near = (
                f" (within {NEAR_LIMIT_KM} km of the limit)"
                if check.car_route_km > limit - NEAR_LIMIT_KM
                else ""
            )
            return Criterion("location", "PASS", f"{detail} <= {limit} km{near}")
        return Criterion("location", "FAIL", f"{detail} > {limit} km{relocation}")
    estimate = _estimate_criterion(job, settings, limit, origin)
    if estimate is not None:
        return estimate
    if (job.location or "").strip():
        address = best_workplace(
            job.description or "",
            posted_by_agency=is_intermediary(job.company),
            job_location=job.location,
        )
        hint = f"; posting states {address.text} ({address.kind.lower()})" if address else ""
        return Criterion(
            "location",
            "DISTANCE_UNVERIFIED",
            f"workplace '{job.location}' known; car route from {origin} not yet checked "
            f"(limit {limit} km){hint}",
        )
    return Criterion("location", "UNKNOWN", "workplace address unknown")


def workplace_doubts(job: JobRecord) -> list[str]:
    """Stage 22 P1: why the BA workplace may not be the real one (agency
    office, head office, one of several locations)."""
    doubts = []
    if is_intermediary(job.company):
        doubts.append("posted by an agency (its address is not the client's workplace)")
    count = getattr(job, "workplace_count", None) or 0
    if count > 1:
        doubts.append(f"{count} work locations, only the first has coordinates")
    own = city_key(job.location)
    text = job.description or ""
    named = {
        city_key(a.city)
        for a in extract_addresses(text, posted_by_agency=is_intermediary(job.company))
        if a.kind == "WORKPLACE"
    } | stated_workplace_towns(text)
    other = sorted(town for town in named if town and own and town != own)
    if other:
        doubts.append(f"posting names another workplace ({', '.join(other)})")
    return doubts


def _estimate_criterion(
    job: JobRecord, settings: CandidateSearchSettings, limit: int, origin: str
) -> Criterion | None:
    """Stage 22 P1: a preliminary estimate from the BA coordinates when no
    car route is stored (`app.agents.geo_estimate`). Ranks the vacancy; the
    car route stays a pre-Bewerbung check (every status here is in _VERIFY)."""
    home = settings.commute_origin
    # getattr: rows read through older query shapes / test doubles lack them
    lat, lon = getattr(job, "workplace_lat", None), getattr(job, "workplace_lon", None)
    if home is None or lat is None or lon is None:
        return None
    estimate = estimate_commute(home.lat, home.lon, lat, lon, limit)
    where = job.location or job.workplace_postal_code or "the posted workplace"
    detail = (
        f"~{estimate.straight_km:.0f} km straight line from {origin} to {where} "
        f"(BA coordinates; a car route is never shorter; limit {limit} km by car)"
    )
    doubts = workplace_doubts(job)
    if doubts:
        return Criterion(
            "location",
            "LOCATION_UNVERIFIED",
            f"{detail}; workplace unverified: {'; '.join(doubts)}",
        )
    state = commutable_state(job.workplace_region)
    if estimate.band == "BEYOND":
        relocation = {state_key(r) for r in settings.relocation_regions}
        if job.workplace_region and state_key(job.workplace_region) in relocation:
            return Criterion(
                "location",
                "ESTIMATE_BEYOND_RELOCATION",
                f"{detail} -- beyond the daily commute, but relocation within "
                f"{job.workplace_region} would be possible; operator decision",
            )
        region = f" in {job.workplace_region}" if state is False else ""
        return Criterion(
            "location",
            "ESTIMATE_BEYOND",
            f"{detail} -- beyond the limit{region}; relocation there is not considered",
        )
    if estimate.band == "WITHIN":
        return Criterion(
            "location",
            "ESTIMATE_WITHIN",
            f"{detail} -- very likely within the limit; check the car route before applying",
        )
    return Criterion(
        "location",
        "ESTIMATE_NEAR_LIMIT",
        f"{detail} -- near the limit; measure the car route",
    )


def salary_criterion(text: str, settings: CandidateSearchSettings) -> Criterion:
    info = extract_salary(text)
    extras = "".join(
        f"; {c.kind.lower().replace('_', ' ')} stated separately (not base pay)"
        for c in info.components[:2]
    )
    if info.yearly_max is None:
        if info.tariff:
            return Criterion(
                "salary",
                "TARIFF",
                "collective pay scale; amount not stated" + extras,
                info.evidence,
            )
        if info.hourly_max is not None:
            return Criterion(
                "salary",
                "HOURLY",
                f"hourly rate {info.hourly_max:,.2f} EUR; no yearly figure assumed" + extras,
                info.evidence,
            )
        if info.negotiable:
            return Criterion("salary", "NEGOTIABLE", "salary by agreement" + extras)
        return Criterion("salary", "UNKNOWN", "no base salary stated" + extras)
    if settings.minimum_salary is None:
        return Criterion("salary", "NOT_CONFIGURED", "no salary expectation configured")
    period = f" (monthly x{info.salaries_per_year})" if info.period == "MONTH" else ""
    low = "up to " if info.yearly_min is None else f"{info.yearly_min:,.0f}-"
    stated = f"{low}{info.yearly_max:,.0f} EUR/year{period}"
    if info.provenance == "JOB_BOARD_ESTIMATE":
        stated += " [job-board estimate, not stated by the employer]"
    stated += extras
    if info.yearly_max >= settings.minimum_salary:
        return Criterion("salary", "MATCH", f"{stated} reaches the expectation", info.evidence)
    negotiable = " (expectation negotiable)" if settings.salary_negotiable else ""
    return Criterion(
        "salary",
        "BELOW",
        f"{stated} below {settings.minimum_salary:,.0f}{negotiable}",
        info.evidence,
    )


_STUDENT_JOB = re.compile(r"werkstudent\w*|working\s+student|studentische\w*", re.IGNORECASE)


def entry_level_criterion(title: str, text: str) -> Criterion:
    seniority = classify_title_seniority(title)
    if seniority.level == "SENIOR" and not entry_level_signals(title, "").positive:
        # Stage 22 P1: the TITLE itself asks for seniority (senior, lead,
        # principal, architect ...); the junior candidate is not a fit.
        return Criterion(
            "entry_level", "SENIOR_TITLE", f"senior title ({seniority.matched_signal})"
        )
    student = _STUDENT_JOB.search(title)
    if student is not None:
        return Criterion(
            "entry_level",
            "STUDENT_JOB",
            f"student job ({student.group(0)}): usually requires enrolment; check eligibility",
        )
    signals = entry_level_signals(title, text)
    if signals.negative and not signals.positive:
        return Criterion("entry_level", "EXPERIENCED", "signals: " + ", ".join(signals.negative))
    if signals.positive and not signals.negative:
        return Criterion("entry_level", "ENTRY_LEVEL", "signals: " + ", ".join(signals.positive))
    if signals.positive and signals.negative:
        return Criterion(
            "entry_level",
            "MIXED",
            f"entry: {', '.join(signals.positive)}; senior: {', '.join(signals.negative)}",
        )
    # No "junior" wording is not a reason to exclude a posting.
    return Criterion("entry_level", "OPEN", "no seniority stated")


_CLAUSE = re.compile(r"[.;\n•]+")
_DEGREE = re.compile(
    r"studium|hochschulabschluss|hochschulstudium|abschluss|bachelor|master|diplom|degree",
    re.IGNORECASE,
)
_IT_FIELD = re.compile(
    r"informatik|computer\s*science|softwaretechnik|software\s*engineering", re.IGNORECASE
)
_OTHER_FIELD = re.compile(
    r"ingenieur|engineering|mint\b|naturwissenschaft|mathematik|physik|elektrotechnik"
    r"|maschinenbau|technisch|technical",
    re.IGNORECASE,
)
_ALTERNATIVE = re.compile(
    r"vergleichbar|(?:ä|ae)hnlich|oder\s+(?:eine\s+)?(?:ausbildung|berufserfahrung)"
    r"|fachinformatiker|quereinst|alternativ|equivalent|similar",
    re.IGNORECASE,
)
# "z. B.", "bzw.", "ggf." must not cut a clause before its alternative.
_ABBREVIATION = re.compile(
    r"\b(?:[A-Za-z]|bzw|ggf|etc|evtl|inkl|ca|mind|vgl|sog|usw|abgeschl)\.", re.IGNORECASE
)
_NOT_REQUIRED = re.compile(
    r"nicht\s+(?:zwingend\s+)?(?:erforderlich|notwendig|n(?:ö|oe)tig|vorausgesetzt|voraussetzung)"
    r"|kein(?:e|en)?\s+(?:muss|voraussetzung|pflicht)|not\s+required|no\s+degree"
    r"|\bkein(?:e|en)?\s+(?:[\w-]+\s+){0,4}?(?:erforderlich|notwendig|n(?:ö|oe)tig|vorausgesetzt)",
    re.IGNORECASE,
)
_QUEREINSTIEG = re.compile(r"quereinst\w*", re.IGNORECASE)
_SOFT = re.compile(r"wünschenswert|wuenschenswert|von\s+vorteil|idealerweise|ein\s+plus", re.I)


def education_criterion(text: str, quereinstieg_suitable: bool | None = None) -> Criterion:
    """Stage 22 P1: what the posting asks for in formal education. Only a
    MANDATORY computer-science degree without any alternative is a gap for
    the candidate (an engineering degree); an engineering/technical field,
    "oder vergleichbare Qualifikation", "Quereinstieg" or the BA flag
    `quereinstiegGeeignet` are not."""
    if quereinstieg_suitable:
        return Criterion("education", "QUEREINSTIEG", "BA: suitable for career changers")
    welcome = _QUEREINSTIEG.search(text or "")
    if welcome is not None:
        return Criterion("education", "QUEREINSTIEG", "career changers welcome", welcome.group(0))
    prose = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", ""), text or "")
    clauses = [c for c in _CLAUSE.split(prose) if _DEGREE.search(c) and not _NOT_REQUIRED.search(c)]
    if not clauses:
        return Criterion("education", "NO_REQUIREMENT", "no degree requirement found")
    it_only = [
        c.strip()
        for c in clauses
        if _IT_FIELD.search(c)
        and not _OTHER_FIELD.search(c)
        and not _ALTERNATIVE.search(c)
        and not _SOFT.search(c)
    ]
    if it_only:
        return Criterion(
            "education",
            "IT_DEGREE_REQUIRED",
            "a computer-science degree is asked without an alternative",
            " ".join(it_only[0].split())[:160],
        )
    if all(_SOFT.search(c) for c in clauses):
        return Criterion("education", "OPTIONAL", "a degree is preferred, not required")
    return Criterion(
        "education", "DEGREE_OR_EQUIVALENT", "degree or an equivalent/technical field accepted"
    )


def _strong_wording(requirement) -> bool:
    """Level, mandatory status and wording all from the ONE German
    requirement the extractor selected (Codex P1 round 3)."""
    return bool(requirement.mandatory and requirement.level in {"C1", "C2"} and requirement.strong)


def german_requirement_is_strong(text: str) -> bool:
    """A MANDATORY German requirement at C level worded as fluency/native or
    a stated C level ("sehr gute" alone is not)."""
    return _strong_wording(extract_german_requirement(text))


_POSTING_TYPES = {
    "ARBEIT": ("EMPLOYMENT", "regular employment"),
    "PRAKTIKUM_TRAINEE": ("TRAINEE_OR_INTERNSHIP", "trainee programme or internship"),
    "SELBSTAENDIGKEIT": (
        "SELF_EMPLOYMENT",
        "self-employment / freelance (BA: often a training provider's course)",
    ),
    "AUSBILDUNG": ("APPRENTICESHIP", "apprenticeship / dual study, not a job for the profile"),
}


def posting_type_criterion(posting_type: str | None) -> Criterion:
    """Stage 22 P1: the BA's own posting type (`stellenangebotsart`)."""
    status, reason = _POSTING_TYPES.get(posting_type or "", ("UNKNOWN", "posting type not stated"))
    return Criterion("posting_type", status, reason, posting_type)


def start_date_criterion(text: str, settings: CandidateSearchSettings) -> Criterion:
    if settings.earliest_start_date is None:
        return Criterion("start_date", "NOT_CONFIGURED", "earliest start date not configured")
    start = settings.earliest_start_date.strftime("%d.%m.%Y")
    if asks_immediate_start(text):
        return Criterion(
            "start_date",
            "EARLIER_REQUESTED",
            f"posting asks for an early start; candidate from {start}",
        )
    return Criterion("start_date", "OK", f"candidate available from {start}")


def duplicate_criterion(session: Session, job: JobRecord) -> Criterion:
    findings = vacancy_findings(session, job)
    if not findings:
        return Criterion("duplicate", "CLEAR", "no application to this vacancy recorded")
    sent = sent_duplicate(findings)
    strongest = sent or findings[0]
    detail = (
        f"{strongest.strength}: application {strongest.application_id} [{strongest.status}] "
        f"({', '.join(strongest.signals)})"
    )
    if sent is not None:
        return Criterion("duplicate", "ALREADY_APPLIED", detail)
    if strongest.strength == "STRONG":
        # An application for this vacancy exists but was not sent yet.
        return Criterion("duplicate", "IN_PREPARATION", detail)
    if verdict(findings) == "POSSIBLE_DUPLICATE":
        return Criterion("duplicate", "POSSIBLE_DUPLICATE", detail)
    return Criterion("duplicate", "CLEAR", detail)


# --- composition --------------------------------------------------------------


def confirmed_german_level(session: Session) -> str:
    profile = get_candidate_profile(session)
    if profile is None:
        return "UNKNOWN"
    for language in profile.languages:
        if normalize_text_identity(language.language) in {"deutsch", "german"}:
            # Same rule as CV generation: a directly asserted AND confirmed level.
            if is_usable_for_generation(language.source, language.confidence):
                return language.level
    return "UNKNOWN"


def _experience_missing(match: CandidateJobMatch | None) -> bool:
    """The (current) matcher analysis lists a stated experience requirement
    as MISSING from the profile."""
    if match is None:
        return False
    # Stage 22 P1 (Codex): only a MANDATORY requirement -- "nice to have:
    # 3 years" is PREFERRED and never a barrier.
    return any(
        r.requirement_type == "EXPERIENCE"
        and r.match_status == "MISSING"
        and r.importance == "REQUIRED"
        for r in match.missing_requirements
    )


def assess_job(session: Session, job_id: int) -> FitAssessment:
    """Read-only: computes every criterion; writes nothing."""
    job = session.get(JobRecord, job_id)
    if job is None:
        raise LookupError(f"job {job_id} does not exist")
    config = load_search_config(session)
    settings = config.search if config else CandidateSearchSettings()
    text = f"{job.title}\n{job.location or ''}\n{job.description or ''}"
    profile = get_candidate_profile(session)
    # Codex Stage 19 review: an analysis of another posting or profile
    # version is not evidence. Stage 22 P1 (Codex): the analysis of the
    # CURRENT identity (posting, profile version, algorithm), not merely the
    # newest row -- after a posting changes A -> B -> A the newest row is B's.
    match_record = (
        get_cached_match(
            session,
            job_id=job_id,
            candidate_profile_version=profile.profile_version,
            job_snapshot_fingerprint=compute_job_snapshot_fingerprint(job),
            algorithm_version=MATCHER_VERSION,
        )
        if profile is not None
        else None
    )
    stale = match_record is None and get_latest_match(session, job_id) is not None
    match = to_candidate_job_match(match_record) if match_record is not None else None

    remote = remote_criterion(text, settings, job)
    german_level = confirmed_german_level(session)
    assessment = FitAssessment(
        job_id=job.id,
        company=job.company,
        title=job.title,
        german_requirement_basis=extract_german_requirement(text).basis,
        german_level_confirmed=german_level != "UNKNOWN",
        experience_missing_in_profile=_experience_missing(match),
        german_requirement_strong=german_requirement_is_strong(text),
        required_years=entry_level_signals(job.title, job.description or "").required_years,
    )
    assessment.criteria = [
        (
            Criterion(
                "skills",
                "NOT_EVALUATED",
                "stored matcher analysis is out of date (posting or profile changed); "
                "run the match again",
            )
            if stale
            else skills_criterion(match)
        ),
        role_criterion(job.title, settings),
        experience_criterion(match, job.title, job.description or ""),
        language_criterion(text, german_level),
        location_criterion(job, settings, remote, latest_commute_check(session, job_id)),
        remote,
        salary_criterion(text, settings),
        entry_level_criterion(job.title, job.description or ""),
        education_criterion(job.description or "", job.quereinstieg_suitable),
        posting_type_criterion(job.posting_type),
        start_date_criterion(text, settings),
        duplicate_criterion(session, job),
    ]
    return assessment
