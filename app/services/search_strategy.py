"""Stage 17C: which Bundesagentur searches one collector run performs.

With a configured candidate search (app/models/candidate_search.py):

1. **commute search** -- `wo` = the home postal code + city, `umkreis` = the
   smallest BA radius step that is at least the car-route limit plus a
   margin. The BA radius is a straight-line distance from the place's
   centre; every workplace whose CAR route from the commute origin is
   within the limit lies within limit + (origin-to-centre distance) in a
   straight line. The configuration requires the origin label to name a
   place inside `home_city`, i.e. the origin lies in the home municipality;
   no German municipality reaches RADIUS_MARGIN_KM (50 km) from its centre
   (Berlin: about 25 km), so this discovers a superset. That bound rests on
   the operator-confirmed origin really lying in the named place -- it is
   not re-derived from the coordinates (that would need geocoding the home
   place). Being
   inside the radius never makes a job "close enough": only a verified car
   route (job_commute_checks) does. Frankfurt, Offenbach, Hanau, Darmstadt,
   Mainz, Wiesbaden and places in neighbouring states are all inside it.
2. **remote search** (only if remote work across Germany is configured) --
   no `wo`, nationwide; only postings whose own BA flag `homeofficemoeglich`
   is true are kept (Stage 22 P1: the former `arbeitszeit=ho` filter returns
   0 results on the v6 API without an error, measured 2026-10-30). A kept
   hit is NOT "remote": it is still assessed for "fully remote, work from the
   home region allowed" from the posting text.

`search_keywords` (configured) gives one commute and one remote search
per term; otherwise BUNDESAGENTUR_SEARCH_KEYWORDS is the single term.

Without a configured search the single legacy query from
BUNDESAGENTUR_SEARCH_KEYWORDS/LOCATION/RADIUS_KM is used unchanged.
"""

import logging
from dataclasses import dataclass

from app.agents.target_role import is_support_admin_term
from app.models.candidate_search import CandidateSearchSettings

logger = logging.getLogger(__name__)

# Radius steps offered by the BA job search; the API documents `umkreis` in km.
BA_RADIUS_STEPS = (10, 15, 20, 25, 50, 100, 150, 200)
# Straight-line margin between the BA place centre and the commute origin.
RADIUS_MARGIN_KM = 50
DEFAULT_MAX_PAGES = 20
REMOTE_MAX_PAGES = 10


@dataclass(frozen=True)
class BundesagenturQuery:
    label: str  # commute | remote | settings
    keywords: str
    location: str
    radius_km: int
    working_time: str = ""
    max_pages: int = DEFAULT_MAX_PAGES
    home_office_only: bool = False
    offer_type: int | None = None  # Stage 23: BA `angebotsart` (1 = employment)
    published_within_days: int | None = None  # Stage 23: BA `veroeffentlichtseit` (7 or 14)
    # Nationwide discovery: keep only development-role titles (the BA home-office flag is never
    # a filter there: the posting text decides).
    target_titles_only: bool = False


def ba_radius_for(limit_km: int) -> int:
    wanted = limit_km + RADIUS_MARGIN_KM
    for step in BA_RADIUS_STEPS:
        if step >= wanted:
            return step
    # Never silently search a radius smaller than the limit.
    raise ValueError(f"car-route limit {limit_km} km exceeds the largest BA radius")


# Stage 24: opt-in discovery profile (never used by the scheduler or the default
# refresh). Terms are the junior / career-changer entry points measured on
# 2026-11-01; every query asks for employment only (`angebotsart=1`), and the
# only freshness window used is 7 days (BA ignores other values). Terms that
# systematically return Elektro / SPS / SAP vacancies ("Junior Automatisierung")
# are deliberately absent -- the fit assessment's domain criterion removes such
# titles afterwards, but they would only waste requests.
DISCOVERY_NEAR_TERMS = (
    "Junior Python",
    "Junior Backend",
    "Junior Softwareentwickler",
    "Python Entwickler",
    "Backend Developer",
    "Anwendungsentwickler",
    "Softwareentwickler Berufseinsteiger",
    "Fachinformatiker Anwendungsentwicklung",
    "Trainee IT",
    "IT Quereinsteiger",
)
DISCOVERY_REMOTE_TERMS = (
    "Junior Python",
    "Junior Backend",
    "IT Quereinsteiger",
    "Trainee IT",
)
# Broad terms are fetched weekly, as the last 7 days only (393 / 45 results,
# below the 1000-result cut-off).
DISCOVERY_REMOTE_WEEKLY_TERMS = ("Softwareentwickler", "Anwendungsentwickler")
DISCOVERY_MAX_PAGES = 5
EMPLOYMENT_ONLY = 1  # BA `angebotsart`: 1 = ARBEIT
PUBLISHED_WEEK = 7  # BA `veroeffentlichtseit`: only 7 and 14 narrow the result


def discovery_queries(settings, search: CandidateSearchSettings | None) -> list[BundesagenturQuery]:
    """The Stage 24 discovery searches; empty unless a commute search is
    configured (the same precondition as the regular strategy)."""
    regular = bundesagentur_queries(settings, search)
    commute = next((q for q in regular if q.label == "commute"), None)
    if commute is None:
        return []
    queries = [
        BundesagenturQuery(
            label="discovery-commute",
            keywords=term,
            location=commute.location,
            radius_km=commute.radius_km,
            max_pages=DISCOVERY_MAX_PAGES,
            offer_type=EMPLOYMENT_ONLY,
        )
        for term in DISCOVERY_NEAR_TERMS
    ]
    if any(q.label == "remote" for q in regular):
        queries += [
            BundesagenturQuery(
                label="discovery-remote",
                keywords=term,
                location="",
                radius_km=0,
                home_office_only=True,
                max_pages=DISCOVERY_MAX_PAGES,
                offer_type=EMPLOYMENT_ONLY,
            )
            for term in DISCOVERY_REMOTE_TERMS
        ]
        queries += [
            BundesagenturQuery(
                label="discovery-remote-weekly",
                keywords=term,
                location="",
                radius_km=0,
                home_office_only=True,
                max_pages=DISCOVERY_MAX_PAGES,
                offer_type=EMPLOYMENT_ONLY,
                published_within_days=PUBLISHED_WEEK,
            )
            for term in DISCOVERY_REMOTE_WEEKLY_TERMS
        ]
    return queries


def _target_terms(terms: list[str]) -> list[str]:
    """Stage 24: the target is software development -- a stored keyword that
    only finds IT-support / administration vacancies is not searched. The drop is
    logged (count only) so a stale stored keyword is never ignored silently."""
    kept = [term for term in terms if not is_support_admin_term(term)]
    if len(kept) != len(terms):
        logger.info(
            "search_terms_excluded_by_target_policy dropped=%s kept=%s",
            len(terms) - len(kept),
            len(kept),
        )
    return kept


def _search_terms(stored: list[str], default: str) -> list[str]:
    """Stored keywords without support/admin terms. If none is left (or none
    was stored), the default is used unless it is a support/admin term; after
    stored keywords existed a blank default is never searched (an empty term
    reads as "every occupation"). Nothing stored + blank default: legacy."""
    target = _target_terms(stored)
    if target:
        return target
    if stored or default.strip():
        # The configured default is filtered like stored keywords; after stored
        # keywords were all dropped a blank default is no term at all.
        return _target_terms([default] if default.strip() else [])
    return [default]  # nothing stored, blank default: legacy behaviour unchanged


def bundesagentur_queries(
    settings, search: CandidateSearchSettings | None
) -> list[BundesagenturQuery]:
    keywords = settings.bundesagentur_search_keywords
    configured = (
        search is not None
        and search.home_city
        and search.max_commute_km is not None
        and search.commute_measure == "CAR_ROUTE"
    )
    if not configured:
        terms = _search_terms(search.search_keywords if search is not None else [], keywords)
        return [
            BundesagenturQuery(
                label="settings",
                keywords=term,
                location=settings.bundesagentur_search_location,
                radius_km=settings.bundesagentur_search_radius_km,
            )
            for term in terms
        ]
    location = " ".join(part for part in (search.home_postal_code, search.home_city) if part)
    terms = _search_terms(search.search_keywords, keywords)
    radius = ba_radius_for(search.max_commute_km)
    queries = [
        BundesagenturQuery(label="commute", keywords=term, location=location, radius_km=radius)
        for term in terms
    ]
    if search.remote_work_scope == "GERMANY":
        queries += [
            BundesagenturQuery(
                label="remote",
                keywords=term,
                location="",
                radius_km=0,
                home_office_only=True,
                max_pages=REMOTE_MAX_PAGES,
            )
            for term in terms
        ]
    return queries


# --- nationwide remote discovery (TRACK B) ----------------------------------------------
# Terms measured live on 2026-11-08 (no `wo`, `angebotsart=1`): the BA `was` search is fuzzy,
# so a narrow term already returns the narrower ones ("Python Entwickler" returned every hit of
# "Junior Softwareentwickler Python", "Junior Fullstack Python", "Trainee Softwareentwicklung").
# Only terms that added vacancies, plus the two framework roles the candidate names, are kept.
NATIONWIDE_TERMS = (
    "Python Entwickler",
    "Backend Developer",
    "Python Backend",
    "Python Developer",
    "Python Automation",
    "Junior Python",
    "Junior Backend Entwickler",
    "Junior Data Engineer Python",
    "Entry Level Python",
    "Django Developer",
    "FastAPI Developer",
)
NATIONWIDE_MAX_PAGES = 5  # 500 hits per term; every measured term is below that
# BA `veroeffentlichtseit` narrows the result only for 7 and 14 (measured 2026-11-01).
NATIONWIDE_WINDOWS = (7, 14)


def nationwide_window_days(days_since_last_success: float | None) -> int | None:
    """The BA freshness window for an incremental run: the smallest effective one that still
    covers the time since the last successful run (one day of margin); None (no window =
    everything) for a first run or a gap longer than 13 days."""
    if days_since_last_success is None:
        return None
    for window in NATIONWIDE_WINDOWS:
        if days_since_last_success <= window - 1:
            return window
    return None


def nationwide_remote_queries(window_days: int | None = None) -> list[BundesagenturQuery]:
    """The TRACK B searches: the whole of Germany (no `wo`, no radius), employment only. The BA
    home-office flag is not a filter here (it is unset on most postings, employer-set, and
    `arbeitszeit=ho` returns nothing): whether a posting is FULLY remote, full time and workable
    from Germany is decided from its text."""
    return [
        BundesagenturQuery(
            label="nationwide",
            keywords=term,
            location="",
            radius_km=0,
            max_pages=NATIONWIDE_MAX_PAGES,
            offer_type=EMPLOYMENT_ONLY,
            published_within_days=window_days,
            target_titles_only=True,
        )
        for term in NATIONWIDE_TERMS
    ]
