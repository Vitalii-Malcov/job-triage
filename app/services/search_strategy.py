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

from dataclasses import dataclass

from app.models.candidate_search import CandidateSearchSettings

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


def ba_radius_for(limit_km: int) -> int:
    wanted = limit_km + RADIUS_MARGIN_KM
    for step in BA_RADIUS_STEPS:
        if step >= wanted:
            return step
    # Never silently search a radius smaller than the limit.
    raise ValueError(f"car-route limit {limit_km} km exceeds the largest BA radius")


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
        terms = (search.search_keywords if search is not None else []) or [keywords]
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
    terms = search.search_keywords or [keywords]
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
