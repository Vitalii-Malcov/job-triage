from app.agents.evidence_quality_classifier import (
    SPARSE_EVIDENCE_THRESHOLD,
    classify_evidence_quality,
)


def test_zero_must_zero_nice_is_sparse():
    assert classify_evidence_quality(0, 0) == "SPARSE"


def test_one_must_zero_nice_is_sparse():
    assert classify_evidence_quality(1, 0) == "SPARSE"


def test_zero_must_one_nice_is_sparse():
    assert classify_evidence_quality(0, 1) == "SPARSE"


def test_two_must_zero_nice_is_sufficient():
    assert classify_evidence_quality(2, 0) == "SUFFICIENT"


def test_zero_must_two_nice_is_sufficient():
    assert classify_evidence_quality(0, 2) == "SUFFICIENT"


def test_one_must_one_nice_is_sufficient():
    # Combined total (1 + 1 = 2) meets the threshold even though neither
    # individual count alone would.
    assert classify_evidence_quality(1, 1) == "SUFFICIENT"


def test_three_must_zero_nice_is_sufficient():
    # The Junior Cyber Security Developer pilot case: 3 must-haves
    # extracted (2 matched, 1 genuinely missing) -- a real, specific gap
    # was identified, not an absence of evidence.
    assert classify_evidence_quality(3, 0) == "SUFFICIENT"


def test_large_counts_are_sufficient():
    assert classify_evidence_quality(6, 2) == "SUFFICIENT"


def test_threshold_constant_is_two():
    # Documents the exact value this module's behavior depends on --
    # mirrors app.agents.job_scorer.MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY.
    assert SPARSE_EVIDENCE_THRESHOLD == 2
