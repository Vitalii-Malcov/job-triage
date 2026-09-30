from app.agents.evidence_quality_classifier import (
    SPARSE_EVIDENCE_THRESHOLD,
    classify_evidence_quality,
)


def test_zero_unique_evidence_is_sparse():
    assert classify_evidence_quality(0) == "SPARSE"


def test_one_unique_evidence_is_sparse():
    assert classify_evidence_quality(1) == "SPARSE"


def test_two_unique_evidence_is_sufficient():
    assert classify_evidence_quality(2) == "SUFFICIENT"


def test_three_unique_evidence_is_sufficient():
    # The Junior Cyber Security Developer pilot case: 3 must-haves
    # extracted (2 matched, 1 genuinely missing) -- a real, specific gap
    # was identified, not an absence of evidence.
    assert classify_evidence_quality(3) == "SUFFICIENT"


def test_large_count_is_sufficient():
    assert classify_evidence_quality(8) == "SUFFICIENT"


def test_threshold_constant_is_two():
    # Documents the exact value this module's behavior depends on --
    # mirrors app.agents.job_scorer.MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY.
    assert SPARSE_EVIDENCE_THRESHOLD == 2
