"""HTTP routes: research (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.routers import common
from app.db.repositories import (
    get_job_by_id,
)
from app.db.session import get_db
from app.models.company_research import (
    CompanyResearchResponse,
    CompanyResearchRunResponse,
    ResearchRequest,
)
from app.providers.base import ProviderNotConfiguredError
from app.security.auth import require_api_key
from app.security.rate_limit import (
    enforce_company_research_rate_limit,
    enforce_rate_limit,
)
from app.services.collector_runner import (
    run_company_research_for_job,
)
from app.services.company_research import (
    AmbiguousCompanyIdentityError,
    CompanyResearchService,
    InvalidCompanyIdentityError,
)
from app.services.offload import run_off_loop

logger = logging.getLogger("app.api.routes")
router = APIRouter()


@router.post(
    "/jobs/{job_id}/research",
    response_model=CompanyResearchRunResponse,
    dependencies=[Depends(require_api_key), Depends(enforce_company_research_rate_limit)],
)
async def run_company_research(
    job_id: int,
    body: ResearchRequest = ResearchRequest(),
    db: Session = Depends(get_db),
) -> CompanyResearchRunResponse:
    settings = common.get_settings()
    try:
        result = await run_off_loop(
            lambda: run_company_research_for_job(
                db, settings, job_id, force_refresh=body.force_refresh
            )
        )
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except InvalidCompanyIdentityError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except AmbiguousCompanyIdentityError as exc:
        # the input (job id) is itself valid, but the identity it
        # resolves to is ambiguous relative to current DB state — 409, not
        # 422/404. Never falls back to returning an arbitrary company.
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    if result.research is None and not result.refresh_succeeded:
        # Total failure: the provider failed and there was no prior good
        # record to fall back to — nothing usable exists, so this must not
        # look like a successful 200 (see CompanyResearchRunResponse
        # docstring). The FAILED row CompanyResearchService already
        # persisted stays available for diagnostics/retry via GET.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=result.error or "Company research failed.",
        )
    return result


@router.get(
    "/jobs/{job_id}/research",
    response_model=CompanyResearchResponse,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def get_company_research(job_id: int, db: Session = Depends(get_db)) -> CompanyResearchResponse:
    job = get_job_by_id(db, job_id)
    if job is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Job not found")

    try:
        result = CompanyResearchService().get_cached(db, job)
    except AmbiguousCompanyIdentityError as exc:
        # GET is a pure cache read and must never arbitrarily pick
        # one of several known-domain companies sharing this job's
        # normalized company name — same controlled 409 as POST.
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Company research not found for this job. POST /jobs/{id}/research to run it.",
        )
    return result
