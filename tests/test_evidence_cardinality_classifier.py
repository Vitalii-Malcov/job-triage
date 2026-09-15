from app.agents.evidence_cardinality_classifier import (
    MINIMUM_UNIQUE_EVIDENCE_SIGNALS,
    classify_evidence_cardinality,
    unique_evidence_signals,
)


def test_one_unique_resolved_must_signal_is_low_cardinality():
    result = classify_evidence_cardinality(["python"], [])
    assert result == "LOW_CARDINALITY"


def test_duplicate_across_categories_counts_once():
    # resolved must=["python"], nice=["python"] -- exactly ONE unique
    # signal, not two.
    signals = unique_evidence_signals(["python"], ["python"])
    assert signals == frozenset({"python"})
    assert classify_evidence_cardinality(["python"], ["python"]) == "LOW_CARDINALITY"


def test_two_distinct_signals_is_sufficient():
    signals = unique_evidence_signals(["python"], ["sql"])
    assert signals == frozenset({"python", "sql"})
    assert classify_evidence_cardinality(["python"], ["sql"]) == "SUFFICIENT"


def test_alias_pair_deduplicates_to_one_signal():
    # "postgres" and "PostgreSQL" are an existing project alias pair
    # (app.agents.job_scorer.ALIASES) that both normalize to
    # "postgresql" -- two different textual spellings of the same
    # underlying skill must count once.
    signals = unique_evidence_signals(["postgres"], ["PostgreSQL"])
    assert signals == frozenset({"postgresql"})
    assert classify_evidence_cardinality(["postgres"], ["PostgreSQL"]) == "LOW_CARDINALITY"


def test_zero_evidence_is_low_cardinality():
    assert classify_evidence_cardinality([], []) == "LOW_CARDINALITY"
    assert unique_evidence_signals([], []) == frozenset()


def test_three_distinct_signals_is_sufficient():
    signals = unique_evidence_signals(["python", "git"], ["docker"])
    assert signals == frozenset({"python", "git", "docker"})
    assert classify_evidence_cardinality(["python", "git"], ["docker"]) == "SUFFICIENT"


def test_all_must_signals_duplicated_in_nice_stays_low_cardinality():
    # A larger category count on both sides that still resolves to a
    # single distinct identity.
    signals = unique_evidence_signals(["Python", "python"], ["PYTHON"])
    assert signals == frozenset({"python"})
    assert classify_evidence_cardinality(["Python", "python"], ["PYTHON"]) == "LOW_CARDINALITY"


def test_minimum_threshold_constant_is_two():
    assert MINIMUM_UNIQUE_EVIDENCE_SIGNALS == 2


def test_blank_and_whitespace_entries_are_ignored():
    signals = unique_evidence_signals(["python", "", "   "], [])
    assert signals == frozenset({"python"})
