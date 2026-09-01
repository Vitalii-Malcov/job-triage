"""Pydantic models for Stage 7B email matching + classification.

INFORMATION ONLY — see app/services/gmail_message_analysis.py's module
docstring for the full hard boundary this subsystem enforces (no send,
no draft, no mailbox mutation, no ApplicationStatus mutation, no
email-derived HTTP, no LLM external action).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

MatchType = Literal["APPLICATION", "JOB_ONLY", "AMBIGUOUS", "UNMATCHED"]
ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]

EmailClassification = Literal[
    "APPLICATION_RECEIVED",
    "REQUEST_FOR_INFORMATION",
    "INTERVIEW_INVITATION",
    "INTERVIEW_RESCHEDULE",
    "REJECTION",
    "OFFER",
    "WITHDRAWAL_OR_POSITION_CLOSED",
    "GENERAL_RECRUITER_MESSAGE",
    "AUTOMATED_NOTIFICATION",
    "OTHER",
    "UNKNOWN",
]


class EvidenceItem(BaseModel):
    """One bounded, structured piece of evidence — never the full email
    body (see MATCH_EVIDENCE_MAX_ITEMS / EVIDENCE_FRAGMENT_MAX_LENGTH in
    app/services/email_matching.py and CLASSIFICATION_EVIDENCE_MAX_ITEMS
    in app/agents/email_classifier.py for the bounds this is built under).
    """

    kind: str
    value: str
    weight: int


class CandidateMatch(BaseModel):
    """One tied top-scoring candidate job for an AMBIGUOUS match result."""

    job_id: int
    score: int
    evidence: list[EvidenceItem]


class GmailMessageAnalysis(BaseModel):
    """POST /gmail/messages/{id}/analyze and
    GET /gmail/messages/{id}/analysis's response — one immutable analysis
    revision (see app.db.models.GmailMessageAnalysisRecord's docstring).
    """

    id: int
    gmail_message_id: int
    analysis_version: int
    match_type: MatchType
    matched_job_id: int | None
    match_confidence: ConfidenceLevel
    match_score: int
    match_evidence: list[EvidenceItem]
    candidate_matches: list[CandidateMatch]
    classification: EmailClassification
    classification_confidence: ConfidenceLevel
    classification_evidence: list[EvidenceItem]
    is_automated: bool
    requires_human_review: bool
    created_at: datetime
