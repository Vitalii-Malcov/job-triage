"""API router: includes the per-domain routers (`app.api.routers.*`).

`router` keeps its former import path (`from app.api.routes import router`).
Handlers that other code imports by name are re-exported below."""

from fastapi import APIRouter

from app.api.routers import (
    applications,
    automation,
    candidate,
    collectors,
    follow_ups,
    gmail,
    health,
    jobs,
    research,
    response_drafts,
    review_packages,
)

router = APIRouter()
# The sub-routers carry no prefix, so their routes are appended as-is: `router.routes` stays a
# flat list of APIRoutes (what introspection, tests and tooling expect), unlike
# `include_router`, which nests the sub-router lazily in current FastAPI.
for _sub in (
    health,
    jobs,
    applications,
    candidate,
    research,
    review_packages,
    collectors,
    automation,
    gmail,
    response_drafts,
    follow_ups,
):
    router.routes.extend(_sub.router.routes)

# historical import path
from app.api.routers.response_drafts import (  # noqa: E402,F401
    send_response_draft_endpoint,
)
