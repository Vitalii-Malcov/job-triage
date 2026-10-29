"""Stage 20: the read-only operator UI (docs/STAGE20_UI_ARCHITECTURE.md).

Every page is a GET that reads through `get_ui_db` (read-only session) and
`app.ui.queries`. The only POSTs are login and logout. No route changes a
job, application, event, profile or configuration, and no route makes an
outbound request (mail, Telegram, portal, vacancy URL).
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.datetime_utils import ensure_utc
from app.ui import queries
from app.ui.auth import (
    COOKIE_NAME,
    NotLoggedInError,
    api_key_matches,
    issue_session_token,
    require_same_origin,
    require_ui_enabled,
    require_ui_session,
)
from app.ui.readonly import get_ui_db

UI_DIR = Path(__file__).resolve().parent
MAX_LOGIN_BODY = 4096
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": under it browsers send `Origin: null` on the
    # same-origin login/logout form POST, which require_same_origin refuses
    # (found in the real-browser check). "same-origin" still sends nothing
    # to other sites.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _zone():
    return queries.operator_zone(get_settings().telegram_daily_digest_timezone)


def _local(value: datetime | None) -> str:
    if value is None:
        return "—"
    return ensure_utc(value).astimezone(_zone()).strftime("%d.%m.%Y %H:%M")


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(UI_DIR / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["local"] = _local
    return env


templates = Jinja2Templates(env=_environment())
router = APIRouter(prefix="/ui", dependencies=[Depends(require_ui_enabled)])


def _render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _query_without(request: Request, *drop: str, **extra) -> str:
    params = {k: v for k, v in request.query_params.items() if k not in drop and v}
    params.update({k: v for k, v in extra.items() if v is not None})
    return urlencode(params)


# --- login -------------------------------------------------------------------


@router.get("/static/style.css", include_in_schema=False)
def stylesheet() -> FileResponse:
    return FileResponse(UI_DIR / "static" / "style.css", media_type="text/css")


async def _bounded_body(request: Request) -> bytes:
    """Codex review A (P2): never buffer more than MAX_LOGIN_BODY bytes of an
    unauthenticated request (declared length first, then the stream)."""
    too_large = HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_LOGIN_BODY:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_LOGIN_BODY:
            raise too_large
        body.extend(chunk)
    return bytes(body)


@router.get("/login", include_in_schema=False)
def login_form(request: Request) -> Response:
    return _render(request, "login.html", error=None)


@router.post("/login", include_in_schema=False, dependencies=[Depends(require_same_origin)])
async def login(request: Request) -> Response:
    from app.security.rate_limit import enforce_ui_login_rate_limit

    enforce_ui_login_rate_limit(request)
    body = await _bounded_body(request)
    submitted = parse_qs(body.decode("utf-8", errors="replace")).get("api_key", [""])[0]
    settings = get_settings()
    if not api_key_matches(submitted, settings.api_key):
        return _render(request, "login.html", status_code=401, error="Invalid key.")
    response = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME,
        issue_session_token(settings.api_key, hours=settings.ui_session_hours),
        max_age=settings.ui_session_hours * 3600,
        path="/ui",
        httponly=True,
        samesite="strict",
        secure=settings.ui_cookie_secure,
    )
    return response


@router.post("/logout", include_in_schema=False, dependencies=[Depends(require_same_origin)])
def logout() -> Response:
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME, path="/ui")
    return response


# --- pages -------------------------------------------------------------------

pages = APIRouter(dependencies=[Depends(require_ui_session)])


@pages.get("/", include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_ui_db)) -> Response:
    summary = queries.dashboard_summary(db, get_settings(), _zone())
    return _render(
        request,
        "dashboard.html",
        summary=summary,
        tiers=queries.FIT_TIERS,
        scan_budget=queries.SCAN_BUDGET,
    )


@pages.get("/vacancies", include_in_schema=False)
def vacancies(
    request: Request,
    db: Session = Depends(get_ui_db),
    q: str = Query("", max_length=200),
    city: str = Query("", max_length=100),
    source: str = Query("", max_length=100),
    recommendation: str = Query("", max_length=30),
    fit_tier: str = Query("", max_length=30),
    applied: str = Query("", pattern="^(|yes|no)$"),
    mode: str = Query("", pattern="^(|remote|hybrid)$"),
    before_id: int | None = Query(None, ge=1),
) -> Response:
    filters = queries.VacancyFilters(
        q=q,
        city=city,
        source=source,
        recommendation=recommendation if recommendation in queries.RECOMMENDATIONS else "",
        fit_tier=fit_tier if fit_tier in queries.FIT_TIERS else "",
        applied=applied,
        mode=mode,
        before_id=before_id,
    )
    page = queries.list_vacancies(db, filters)
    next_query = (
        _query_without(request, "before_id", before_id=str(page.next_before_id))
        if page.next_before_id
        else None
    )
    return _render(
        request,
        "vacancies.html",
        page=page,
        filters=filters,
        sources=queries.vacancy_sources(db),
        tiers=queries.FIT_TIERS,
        recommendations=queries.RECOMMENDATIONS,
        next_query=next_query,
        first_query=_query_without(request, "before_id"),
        budget=queries.SCAN_BUDGET,
    )


@pages.get("/vacancies/{job_id}", include_in_schema=False)
def vacancy(request: Request, job_id: int, db: Session = Depends(get_ui_db)) -> Response:
    detail = queries.vacancy_detail(db, job_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _render(request, "vacancy_detail.html", d=detail)


@pages.get("/applications", include_in_schema=False)
def applications(
    request: Request,
    db: Session = Depends(get_ui_db),
    status_filter: str = Query("", alias="status", max_length=30),
) -> Response:
    from app.models.application_lifecycle import ApplicationLifecycleStatus

    return _render(
        request,
        "applications.html",
        applications=queries.list_applications(db, status_filter),
        statuses=[s.value for s in ApplicationLifecycleStatus],
        selected=status_filter,
    )


@pages.get("/applications/{application_id}", include_in_schema=False)
def application(
    request: Request, application_id: int, db: Session = Depends(get_ui_db)
) -> Response:
    detail = queries.application_detail(db, application_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _render(
        request,
        "application_detail.html",
        a=detail.application,
        events=detail.events,
        url=queries.safe_external_url(detail.application.destination_url),
    )


@pages.get("/inbox", include_in_schema=False)
def inbox(request: Request, db: Session = Depends(get_ui_db)) -> Response:
    return _render(request, "inbox.html", box=queries.inbox(db, get_settings()))


@pages.get("/follow-ups", include_in_schema=False)
def follow_ups(request: Request, db: Session = Depends(get_ui_db)) -> Response:
    return _render(request, "follow_ups.html", f=queries.follow_ups(db, _zone()))


router.include_router(pages)


# --- wiring ------------------------------------------------------------------


def install_ui(app: FastAPI) -> None:
    app.include_router(router)

    @app.exception_handler(NotLoggedInError)
    async def _to_login(_request: Request, _exc: NotLoggedInError) -> Response:
        return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.middleware("http")
    async def _ui_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response
