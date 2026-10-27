"""Stage 17C: which Bundesagentur searches one collector run performs.

With a configured candidate search (app/models/candidate_search.py):

1. **commute search** -- `wo` = the home postal code + city, `umkreis` = the
   smallest BA radius step that is at least the car-route limit plus a
   margin. The BA radius is a straight-line distance from the place's
   centre; every workplace whose CAR route is within the limit is also
   within that straight-line radius, so this discovers a superset. Being
   inside the radius never makes a job "close enough": only a verified car
   route (job_commute_checks) does. Frankfurt, Offenbach, Hanau, Darmstadt,
   Mainz, Wiesbaden and places in neighbouring states are all inside it.
2. **remote search** (only if remote work across Germany is configured) --
   no `wo`, `arbeitszeit=ho` (BA: Heim-/Telearbeit), fewer pages. A hit is
   still assessed for "fully remote, work from the home region allowed".

Without a configured search the single legacy query from
BUNDESAGENTUR_SEARCH_KEYWORDS/LOCATION/RADIUS_KM is used unchanged.
"""

from dataclasses import dataclass

from app.models.candidate_search import CandidateSearchSettings

# Radius steps offered by the BA job search; the API documents `umkreis` in km.
BA_RADIUS_STEPS = (10, 15, 20, 25, 50, 100, 150, 200)
# Straight-line margin between the BA place centre and the commute origin.
RADIUS_MARGIN_KM = 5
DEFAULT_MAX_PAGES = 20
REMOTE_MAX_PAGES = 5
HOME_OFFICE = "ho"


@dataclass(frozen=True)
class BundesagenturQuery:
    label: str  # commute | remote | settings
    keywords: str
    location: str
    radius_km: int
    working_time: str = ""
    max_pages: int = DEFAULT_MAX_PAGES


def ba_radius_for(limit_km: int) -> int:
    wanted = limit_km + RADIUS_MARGIN_KM
    return next((step for step in BA_RADIUS_STEPS if step >= wanted), BA_RADIUS_STEPS[-1])


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
        return [
            BundesagenturQuery(
                label="settings",
                keywords=keywords,
                location=settings.bundesagentur_search_location,
                radius_km=settings.bundesagentur_search_radius_km,
            )
        ]
    location = " ".join(part for part in (search.home_postal_code, search.home_city) if part)
    queries = [
        BundesagenturQuery(
            label="commute",
            keywords=keywords,
            location=location,
            radius_km=ba_radius_for(search.max_commute_km),
        )
    ]
    if search.remote_work_scope == "GERMANY":
        queries.append(
            BundesagenturQuery(
                label="remote",
                keywords=keywords,
                location="",
                radius_km=0,
                working_time=HOME_OFFICE,
                max_pages=REMOTE_MAX_PAGES,
            )
        )
    return queries
