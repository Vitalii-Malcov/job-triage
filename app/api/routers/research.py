"""HTTP routes: research (included by `app.api.routes`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.orm import Session

from app.api.http_errors import http_errors
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
    # An ambiguous identity is a 409, not 422/404: the input (job id) is valid, but the identity
    # it resolves to is ambiguous relative to current DB state. Never falls back to an arbitrary
    # company.
    with http_errors(
        (
            (ProviderNotConfiguredError, http_status.HTTP_503_SERVICE_UNAVAILABLE, str),
            (InvalidCompanyIdentityError, http_status.HTTP_422_UNPROCESSABLE_CONTENT, str),
            (AmbiguousCompanyIdentityError, http_status.HTTP_409_CONFLICT, str),
        )
    ):
        result = await run_off_loop(
            lambda: run_company_research_for_job(
                db, settings, job_id, force_refresh=body.force_refresh
            )
        )

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

    # GET is a pure cache read and must never arbitrarily pick one of several known-domain companies
    # sharing this job's normalized company name: the same controlled 409 as POST.
    with http_errors(((AmbiguousCompanyIdentityError, http_status.HTTP_409_CONFLICT, str),)):
        result = CompanyResearchService().get_cached(db, job)
    if result is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Company research not found for this job. POST /jobs/{id}/research to run it.",
        )
    return result
