"""Stage 20: everything the read-only UI shows, computed from existing services.

No business rule lives here (docs/STAGE20_UI_ARCHITECTURE.md, boundary 3):
fit tier / decision / criteria come from `assess_job`, duplicates from
`application_guard`, the inbox from the Stage 15 response processor in dry
run over stored messages, follow-up dates from the tracker's persisted
`next_followup_at`. This module only filters, groups and pages.

Stage 21: once the vacancy assessment cache (`job_fit_assessments`) has been
filled, the fit-tier / applied / work-mode filters and the dashboard tier
counts read it instead of running `assess_job` under a scan budget. Only
FRESH rows (stored input key == current key, `app/services/fit_cache.py`)
count; stale and not-assessed vacancies are reported as such, never under
their old tier. Without the table, or while it is empty, the Stage 20 path
with its scan budget stays in place.

A value that cannot be computed is reported as UNKNOWN / NOT_CONFIGURED with
a reason, never as 0 (boundary 4).
"""

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import application_repository as app_repo
from app.db.datetime_utils import ensure_utc
from app.db.job_commute_repository import latest_commute_check
from app.db.models import (
    ApplicationRecord,
    JobCommuteCheckRecord,
    JobFitAssessmentRecord,
    JobRecord,
)
from app.domain.application_lifecycle import is_terminal
from app.models.application_lifecycle import Application, ApplicationDetail
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services import fit_cache
from app.services.application_duplicates import DuplicateFinding
from app.services.application_guard import vacancy_findings
from app.services.application_response_processor import (
    NEEDS_OPERATOR_REVIEW,
    ApplicationResponseProcessor,
    ResponseTrackingResult,
    applicant_addresses,
    monitored_account_keys,
    stored_inbound_messages,
)
from app.services.application_tracker import ApplicationTracker
from app.services.fit_cache import applied, work_mode
from app.services.job_fit_assessment import FitAssessment, assess_job

PAGE_SIZE = 25
# assess_job costs ~17 ms; one request never assesses more than this.
SCAN_BUDGET = 200
DASHBOARD_WINDOW_DAYS = 14
INBOX_LOOKBACK_DAYS = 30
FIT_TIERS = ("GOOD_MATCH", "PARTIAL_MATCH", "NEEDS_OPERATOR", "NOT_SUITABLE")
RECOMMENDATIONS = ("APPLY", "MAYBE", "SKIP", "NEEDS_ENRICHMENT")
# remote criterion status -> work mode shown / filtered in the UI
_WAITING = frozenset({S.SUBMITTED, S.RECEIVED, S.UNDER_REVIEW})


def operator_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Berlin")


def safe_external_url(url: str | None) -> str | None:
    """Only http(s) URLs become links; the UI never fetches them."""
    if not url:
        return None
    lowered = url.strip().lower()
    if lowered.startswith(("http://", "https://")) and len(lowered) > len("https://"):
        return url.strip()
    return None


# --- vacancies ---------------------------------------------------------------


@dataclass(frozen=True)
class VacancyFilters:
    q: str = ""
    city: str = ""
    source: str = ""
    recommendation: str = ""
    fit_tier: str = ""
    applied: str = ""  # "", "yes", "no"
    mode: str = ""  # "", "remote", "hybrid"
    before_id: int | None = None

    @property
    def computed(self) -> bool:
        """Filters that need assess_job (scan budget applies)."""
        return bool(self.fit_tier or self.applied or self.mode)

    def accepts(self, assessment: FitAssessment) -> bool:
        if self.fit_tier and assessment.fit_tier != self.fit_tier:
            return False
        if self.applied and applied(assessment) != (self.applied == "yes"):
            return False
        if self.mode and work_mode(assessment) != self.mode.upper():
            return False
        return True


@dataclass(frozen=True)
class VacancyRow:
    job: JobRecord
    assessment: FitAssessment
    url: str | None

    @property
    def work_mode(self) -> str:
        return work_mode(self.assessment)

    @property
    def applied(self) -> bool:
        return applied(self.assessment)


@dataclass(frozen=True)
class VacancyPage:
    rows: list[VacancyRow]
    next_before_id: int | None
    scanned: int
    # True when a computed filter stopped at SCAN_BUDGET: older rows were not
    # looked at, so "no more results" must not be claimed.
    budget_exhausted: bool
    # Stage 21: the computed filters were answered from fresh cache rows.
    from_cache: bool = False


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _vacancy_query(filters: VacancyFilters):
    query = select(JobRecord)
    if filters.q:
        pattern = _like(filters.q.strip())
        query = query.where(
            or_(
                JobRecord.title.ilike(pattern, escape="\\"),
                JobRecord.company.ilike(pattern, escape="\\"),
                JobRecord.description.ilike(pattern, escape="\\"),
            )
        )
    if filters.city:
        query = query.where(JobRecord.location.ilike(_like(filters.city.strip()), escape="\\"))
    if filters.source:
        query = query.where(JobRecord.source == filters.source)
    if filters.recommendation:
        query = query.where(JobRecord.recommendation == filters.recommendation)
    if filters.before_id is not None:
        query = query.where(JobRecord.id < filters.before_id)
    return query.order_by(JobRecord.id.desc())


def _iter_jobs(db: Session, query, batch: int) -> Iterator[JobRecord]:
    last_id: int | None = None
    while True:
        page_query = query if last_id is None else query.where(JobRecord.id < last_id)
        jobs = db.scalars(page_query.limit(batch)).all()
        yield from jobs
        if len(jobs) < batch:
            return
        last_id = jobs[-1].id


def list_vacancies(
    db: Session,
    filters: VacancyFilters,
    *,
    assess: Callable[[Session, int], FitAssessment] = assess_job,
    page_size: int = PAGE_SIZE,
    scan_budget: int = SCAN_BUDGET,
) -> VacancyPage:
    query = _vacancy_query(filters)
    if filters.computed and fit_cache.cache_in_use(db):
        return _list_from_cache(db, filters, query, assess=assess, page_size=page_size)
    if not filters.computed:
        jobs = db.scalars(query.limit(page_size + 1)).all()
        more = len(jobs) > page_size
        jobs = jobs[:page_size]
        rows = [VacancyRow(j, assess(db, j.id), safe_external_url(j.url)) for j in jobs]
        return VacancyPage(rows, jobs[-1].id if more and jobs else None, len(jobs), False)

    rows: list[VacancyRow] = []
    scanned = 0
    last_scanned: int | None = None
    exhausted_source = True
    for job in _iter_jobs(db, query, batch=page_size * 2):
        if len(rows) == page_size or scanned == scan_budget:
            exhausted_source = False
            break
        scanned += 1
        last_scanned = job.id
        assessment = assess(db, job.id)
        if filters.accepts(assessment):
            rows.append(VacancyRow(job, assessment, safe_external_url(job.url)))
    budget_exhausted = not exhausted_source and len(rows) < page_size
    next_before = None if exhausted_source else last_scanned
    return VacancyPage(rows, next_before, scanned, budget_exhausted)


def _list_from_cache(
    db: Session,
    filters: VacancyFilters,
    query,
    *,
    assess: Callable[[Session, int], FitAssessment],
    page_size: int,
) -> VacancyPage:
    """Computed filters as SQL on the cache. A candidate counts only while
    its row is FRESH; the page rows are then assessed live (at most
    page_size, ~17 ms each), so the detail shown is always current."""
    query = query.join(JobFitAssessmentRecord, JobFitAssessmentRecord.job_id == JobRecord.id)
    if filters.fit_tier:
        query = query.where(JobFitAssessmentRecord.fit_tier == filters.fit_tier)
    if filters.applied:
        query = query.where(JobFitAssessmentRecord.applied.is_(filters.applied == "yes"))
    if filters.mode:
        query = query.where(JobFitAssessmentRecord.work_mode == filters.mode.upper())
    query = query.add_columns(JobFitAssessmentRecord.input_key)
    inputs = fit_cache.global_inputs(db)
    rows: list[VacancyRow] = []
    scanned = 0
    last_id: int | None = None
    batch = page_size * 2
    while True:
        page_query = query if last_id is None else query.where(JobRecord.id < last_id)
        candidates = db.execute(page_query.limit(batch)).all()
        current = fit_cache.current_keys(db, [job.id for job, _ in candidates], inputs)
        for job, stored_key in candidates:
            if len(rows) == page_size:
                return VacancyPage(rows, last_id, scanned, False, from_cache=True)
            scanned += 1
            last_id = job.id
            if stored_key != current.get(job.id):
                continue  # STALE: never listed under its old tier
            assessment = assess(db, job.id)
            if filters.accepts(assessment):
                rows.append(VacancyRow(job, assessment, safe_external_url(job.url)))
        if len(candidates) < batch:
            return VacancyPage(rows, None, scanned, False, from_cache=True)


def vacancy_sources(db: Session) -> list[str]:
    return list(db.scalars(select(JobRecord.source).distinct().order_by(JobRecord.source)))


@dataclass(frozen=True)
class VacancyDetail:
    job: JobRecord
    assessment: FitAssessment
    url: str | None
    findings: list[DuplicateFinding]
    route: JobCommuteCheckRecord | None
    # Stage 21: FRESH / STALE / NOT_ASSESSED, None without the cache table.
    cache_state: str | None = None


def vacancy_detail(db: Session, job_id: int) -> VacancyDetail | None:
    job = db.get(JobRecord, job_id)
    if job is None:
        return None
    return VacancyDetail(
        job=job,
        assessment=assess_job(db, job_id),
        url=safe_external_url(job.url),
        findings=vacancy_findings(db, job),
        route=latest_commute_check(db, job_id),
        cache_state=_detail_cache_state(db, job_id),
    )


def _detail_cache_state(db: Session, job_id: int) -> str | None:
    if not fit_cache.cache_table_present(db):
        return None
    stored = fit_cache.cached_keys(db, [job_id]).get(job_id)
    return fit_cache.cache_state(stored, fit_cache.current_keys(db, [job_id]).get(job_id))


# --- applications ------------------------------------------------------------


def list_applications(db: Session, status: str = "") -> list[Application]:
    """Every application (complete keyset scan), newest status change first."""
    wanted = S(status) if status in S.__members__ else None
    applications = [
        app_repo.to_application(r)
        for r in app_repo.iter_applications(db)
        if wanted is None or r.current_status == wanted
    ]
    applications.sort(key=lambda a: (a.last_status_changed_at, a.id), reverse=True)
    return applications


def application_detail(db: Session, application_id: int) -> ApplicationDetail | None:
    if app_repo.get_application(db, application_id) is None:
        return None
    return ApplicationTracker().get_application_detail(db, application_id)


# --- inbox (operator review queue) ----------------------------------------------


@dataclass(frozen=True)
class Inbox:
    state: str  # "OK" or "NOT_CONFIGURED"
    reason: str
    since: datetime
    results: list[ResponseTrackingResult] = field(default_factory=list)

    @property
    def needs_review(self) -> list[ResponseTrackingResult]:
        return [r for r in self.results if r.operator_review == NEEDS_OPERATOR_REVIEW]

    @property
    def other(self) -> list[ResponseTrackingResult]:
        return [r for r in self.results if r.operator_review != NEEDS_OPERATOR_REVIEW]


def inbox(db: Session, settings, *, now: datetime | None = None) -> Inbox:
    """Dry run of the Stage 15 processor over already stored mail: nothing
    is recorded and IMAP is never contacted."""
    since = (now or datetime.now(UTC)) - timedelta(days=INBOX_LOOKBACK_DAYS)
    if not settings.gmail_username:
        return Inbox("NOT_CONFIGURED", "GMAIL_USERNAME is not set; no stored mailbox", since)
    messages = stored_inbound_messages(db, settings, since=since)
    results = ApplicationResponseProcessor().process(
        db,
        messages,
        apply=False,
        applicant_addresses=applicant_addresses(settings),
        monitored_account_keys=monitored_account_keys(settings),
    )
    return Inbox("OK", f"{len(messages)} stored inbound message(s) checked", since, results)


# --- follow-ups --------------------------------------------------------------


@dataclass(frozen=True)
class FollowUp:
    application: Application
    due_on: date | None  # local date in the operator's time zone


@dataclass(frozen=True)
class FollowUps:
    today: date
    overdue: list[FollowUp]
    due_today: list[FollowUp]
    next_7_days: list[FollowUp]
    later: list[FollowUp]
    # waiting for an answer but no follow-up date recorded
    undated: list[FollowUp]

    @property
    def due_count(self) -> int:
        return len(self.overdue) + len(self.due_today)


def follow_ups(db: Session, zone: ZoneInfo, *, now: datetime | None = None) -> FollowUps:
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    buckets: dict[str, list[FollowUp]] = {
        k: [] for k in ("overdue", "today", "week", "later", "undated")
    }
    for record in app_repo.iter_applications(db):
        application = app_repo.to_application(record)
        status = application.current_status
        if is_terminal(status):
            continue
        if application.next_followup_at is None:
            if status in _WAITING:
                buckets["undated"].append(FollowUp(application, None))
            continue
        due = ensure_utc(application.next_followup_at).astimezone(zone).date()
        item = FollowUp(application, due)
        if due < today:
            buckets["overdue"].append(item)
        elif due == today:
            buckets["today"].append(item)
        elif due <= today + timedelta(days=7):
            buckets["week"].append(item)
        else:
            buckets["later"].append(item)
    for items in buckets.values():
        items.sort(key=lambda f: (f.due_on or date.max, f.application.id))
    return FollowUps(
        today,
        buckets["overdue"],
        buckets["today"],
        buckets["week"],
        buckets["later"],
        buckets["undated"],
    )


# --- dashboard ---------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    value: int | None
    state: str = "OK"  # OK / UNKNOWN / NOT_CONFIGURED
    reason: str = ""


@dataclass(frozen=True)
class Dashboard:
    jobs_total: int
    jobs_last_24h: int
    jobs_last_7d: int
    window_days: int
    tiers: dict[str, Metric]
    applications_by_status: dict[str, int]
    applications_total: int
    inbox_review: Metric
    follow_ups_due: int
    follow_ups_week: int
    # Stage 21: set when the tier counts come from the assessment cache.
    tiers_all: dict[str, Metric] | None = None
    cache: "CacheOverview | None" = None
    # MISSING / EMPTY: the counts above are the Stage 20 live scan, and the
    # dashboard tells the operator how to fill the cache (Codex audit).
    cache_status: fit_cache.CacheStatus = "IN_USE"


@dataclass(frozen=True)
class CacheOverview:
    fresh: int
    stale: int
    not_assessed: int


def _cached_tier_counts(
    db: Session, since: datetime
) -> tuple[dict[str, Metric], dict[str, Metric], CacheOverview]:
    """Tier counts over the window and over all vacancies, from FRESH rows
    only (SQL and hashing; no assess_job)."""
    current = fit_cache.current_keys(db)
    stored = dict(
        (job_id, (key, tier))
        for job_id, key, tier in db.execute(
            select(
                JobFitAssessmentRecord.job_id,
                JobFitAssessmentRecord.input_key,
                JobFitAssessmentRecord.fit_tier,
            )
        ).all()
    )
    in_window = set(db.scalars(select(JobRecord.id).where(JobRecord.first_seen_at >= since)))
    states: Counter = Counter()
    window: Counter = Counter()
    window_open: Counter = Counter()
    every: Counter = Counter()
    for job_id, key in current.items():
        stored_key, tier = stored.get(job_id, (None, None))
        state = fit_cache.cache_state(stored_key, key)
        states[state] += 1
        if state == "FRESH":
            every[tier] += 1
            if job_id in in_window:
                window[tier] += 1
        elif job_id in in_window:
            window_open[state] += 1
    overview = CacheOverview(states["FRESH"], states["STALE"], states["NOT_ASSESSED"])
    window_reason = ""
    if window_open:
        window_reason = (
            f"excludes {window_open['STALE']} stale and {window_open['NOT_ASSESSED']} "
            "not assessed vacancies in the window"
        )
    all_reason = ""
    if overview.stale or overview.not_assessed:
        all_reason = (
            f"excludes {overview.stale} stale and {overview.not_assessed} not assessed vacancies"
        )
    return (
        {t: Metric(window.get(t, 0), "OK", window_reason) for t in FIT_TIERS},
        {t: Metric(every.get(t, 0), "OK", all_reason) for t in FIT_TIERS},
        overview,
    )


def _count_since(db: Session, since: datetime) -> int:
    return db.scalar(select(func.count(JobRecord.id)).where(JobRecord.first_seen_at >= since)) or 0


def _tier_counts(
    db: Session, since: datetime, assess: Callable[[Session, int], FitAssessment], budget: int
) -> dict[str, Metric]:
    ids = db.scalars(
        select(JobRecord.id)
        .where(JobRecord.first_seen_at >= since)
        .order_by(JobRecord.id.desc())
        .limit(budget + 1)
    ).all()
    if len(ids) > budget:
        reason = f"more than {budget} vacancies in the window; use the filtered vacancy list"
        return {tier: Metric(None, "UNKNOWN", reason) for tier in FIT_TIERS}
    counts = Counter(assess(db, job_id).fit_tier for job_id in ids)
    return {tier: Metric(counts.get(tier, 0)) for tier in FIT_TIERS}


def dashboard_summary(
    db: Session,
    settings,
    zone: ZoneInfo,
    *,
    now: datetime | None = None,
    assess: Callable[[Session, int], FitAssessment] = assess_job,
    scan_budget: int = SCAN_BUDGET,
) -> Dashboard:
    now = now or datetime.now(UTC)
    by_status = Counter(
        dict(
            db.execute(
                select(ApplicationRecord.current_status, func.count(ApplicationRecord.id)).group_by(
                    ApplicationRecord.current_status
                )
            ).all()
        )
    )
    box = inbox(db, settings, now=now)
    inbox_metric = (
        Metric(len(box.needs_review)) if box.state == "OK" else Metric(None, box.state, box.reason)
    )
    due = follow_ups(db, zone, now=now)
    since = now - timedelta(days=DASHBOARD_WINDOW_DAYS)
    tiers_all = cache = None
    cache_status = fit_cache.cache_status(db)
    if cache_status == "IN_USE":
        tiers, tiers_all, cache = _cached_tier_counts(db, since)
    else:
        tiers = _tier_counts(db, since, assess, scan_budget)
    return Dashboard(
        jobs_total=db.scalar(select(func.count(JobRecord.id))) or 0,
        jobs_last_24h=_count_since(db, now - timedelta(hours=24)),
        jobs_last_7d=_count_since(db, now - timedelta(days=7)),
        window_days=DASHBOARD_WINDOW_DAYS,
        tiers=tiers,
        applications_by_status={s.value: by_status.get(s.value, 0) for s in S},
        applications_total=sum(by_status.values()),
        inbox_review=inbox_metric,
        follow_ups_due=due.due_count,
        follow_ups_week=len(due.next_7_days),
        tiers_all=tiers_all,
        cache=cache,
        cache_status=cache_status,
    )
