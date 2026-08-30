from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# PENDING: no research attempted yet (transient — see
# app/services/company_research.py; should not normally be visible outside
# a synthetic record).
# PARTIAL: job-data-only research succeeded. This is the only "success"
# status Company Research v1 can honestly produce — its one provider
# (JobDataCompanyResearchProvider) has no network access and therefore no
# way to gather genuine company-level facts, only vacancy-scoped ones. There
# is deliberately no "COMPLETE" status in v1: claiming complete research
# from a single job posting would be dishonest. A future network-capable
# provider can introduce COMPLETE when it exists.
# FAILED: the provider itself raised; see CompanyResearchService's
# failure-isolation contract for how this interacts with a prior good record.
ResearchStatus = Literal["PENDING", "PARTIAL", "FAILED"]

# SUCCESS | FAILED — outcome of the most recent research *attempt*,
# independent of research_status/researched_at above (see
# CompanyResearchRecord.last_attempt_status in app/db/models.py).
AttemptStatus = Literal["SUCCESS", "FAILED"]

# FACT: directly supported by a specific source (source_url set).
# INFERENCE: a logical deduction from FACTs, not itself directly sourced.
# UNKNOWN: explicitly could not be determined — recorded so callers can tell
# "we checked and don't know" apart from "we never checked".
EvidenceType = Literal["FACT", "INFERENCE", "UNKNOWN"]


class Evidence(BaseModel):
    type: EvidenceType
    claim: str = Field(min_length=1)
    source_url: str | None = None
    source_title: str | None = None


class CompanyResearchData(BaseModel):
    """Content fields: what a provider returns, and what gets persisted.

    Every field with no supporting evidence must stay None/[] rather than be
    guessed — see the Evidence-first requirement documented on
    CompanyResearchProvider (app/providers/base.py).
    """

    company_name: str = Field(min_length=1)
    company_domain: str | None = None
    industry: str | None = None
    headquarters: str | None = None
    company_size: str | None = None
    short_summary: str = ""
    products_or_services: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    hiring_signals: list[str] = Field(default_factory=list)
    relevant_facts: list[str] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    research_status: ResearchStatus = "PENDING"
    provider_name: str = Field(min_length=1)


class CompanyResearchResponse(CompanyResearchData):
    """Full representation of a persisted research record — what GET
    returns, and what's embedded in CompanyResearchRunResponse.research.
    """

    id: int
    researched_at: datetime | None
    last_attempt_at: datetime | None
    last_attempt_status: AttemptStatus | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ResearchRequest(BaseModel):
    force_refresh: bool = False


class CompanyResearchRunResponse(BaseModel):
    """What POST /jobs/{id}/research returns — makes the refresh outcome
    explicit rather than leaving the caller to guess it from research_status
    alone (a Codex review finding: a bare 200 with stale content looked
    indistinguishable from a fresh success). See
    CompanyResearchService.get_or_run for exactly when each combination of
    these fields is produced.

    - Cache hit, no force_refresh: refresh_attempted=False,
      refresh_succeeded=True, refresh_superseded=False, served_stale=False.
    - Miss/stale/force_refresh, provider succeeds and its result becomes
      canonical: refresh_attempted=True, refresh_succeeded=True,
      refresh_superseded=False, served_stale=False.
    - Refresh attempted, the provider succeeded, but a *concurrent* refresh
      already committed a newer result first (RR-M-03 —
      app.db.repositories.CompanyResearchWriteOutcome.SUPERSEDED): this
      caller's own result was never persisted, only the concurrent winner's
      was. refresh_attempted=True, refresh_succeeded=False,
      refresh_superseded=True, served_stale=False, research=<the winning
      concurrent result>. Not a provider error and not stale cache — the
      canonical research is in fact *newer* than what this caller produced.
      HTTP 200 is appropriate: there is fresh, usable research to show.
    - Refresh attempted but the provider itself failed (or raised
      ProviderNotConfiguredError, handled separately), and a *usable* prior
      result exists (research_status == "PARTIAL" with a researched_at —
      see CompanyResearchService._is_usable_research; a previous FAILED/
      PENDING diagnostic row does not count, RR-M-02): refresh_attempted=
      True, refresh_succeeded=False, refresh_superseded=False,
      served_stale=True, research=<the untouched prior good record>, error
      set. HTTP 200 — there is usable (if stale) data to show.
    - Refresh attempted, the provider failed, and no *usable* prior result
      exists (either nothing was ever persisted, or the only record on file
      is itself a FAILED/PENDING diagnostic row from an earlier failed
      attempt — RR-M-02): refresh_attempted=True, refresh_succeeded=False,
      refresh_superseded=False, served_stale=False, research=None, error
      set. Routed to HTTP 502 by app/api/routes.py — there is nothing
      usable to return as a 200, and a repeated failure must keep returning
      502, never silently look like a stale-but-successful 200.
    """

    research: CompanyResearchResponse | None
    refresh_attempted: bool
    refresh_succeeded: bool
    refresh_superseded: bool = False
    served_stale: bool
    error: str | None = None
