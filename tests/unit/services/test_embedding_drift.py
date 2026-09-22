"""The corpus is only searchable by the model that wrote it.

Nine tables carry one `embedding` column each and no column says which model
produced it. Qodo and codestral-embed both emit 1536 dimensions, so swapping
one for the other without a reindex raises no error anywhere: pgvector keeps
answering, and every cosine distance silently becomes noise. These tests pin
the verdict logic that turns a sample of stored-versus-fresh similarities into
a decision an operator can act on.
"""

from __future__ import annotations

import pytest

from brain_v42.services.embedding_drift import (
    DriftVerdict,
    SampleComparison,
    classify_drift,
    final_exit_code,
)


def _samples(*similarities: float) -> list[SampleComparison]:
    return [
        SampleComparison(entity_type="learning", entity_id=f"id-{i}", similarity=s)
        for i, s in enumerate(similarities)
    ]


def test_a_corpus_written_by_the_configured_model_matches() -> None:
    report = classify_drift(_samples(0.999, 1.0, 0.998, 0.9995), threshold=0.95)

    assert report.verdict is DriftVerdict.MATCH
    assert report.exit_code == 0
    assert report.sampled == 4


def test_a_corpus_written_by_another_model_is_drift() -> None:
    """Two unrelated 1536-dim models agree by chance, not by construction."""
    report = classify_drift(_samples(0.03, -0.01, 0.11, 0.07), threshold=0.95)

    assert report.verdict is DriftVerdict.DRIFT
    assert report.exit_code == 1


def test_an_empty_sample_is_unmeasurable_and_never_a_pass() -> None:
    """Fail closed. No embedded row means the question was not answered, and a
    silent 0 here would green-light a provider switch on no evidence at all."""
    report = classify_drift([], threshold=0.95)

    assert report.verdict is DriftVerdict.UNMEASURABLE
    assert report.exit_code == 2


def test_a_minority_of_edited_rows_does_not_raise_a_false_alarm() -> None:
    """`content_updated_at` moves without the vector being rewritten, so some
    rows legitimately drift on their own. The verdict reads the median, not the
    worst row, or every run after an ordinary edit would cry provider swap."""
    report = classify_drift(_samples(0.999, 0.998, 0.02, 0.997, 0.996), threshold=0.95)

    assert report.verdict is DriftVerdict.MATCH
    assert report.outliers == 1


def test_a_majority_of_stale_rows_is_still_reported_as_drift() -> None:
    report = classify_drift(_samples(0.999, 0.02, 0.01, 0.03, 0.998), threshold=0.95)

    assert report.verdict is DriftVerdict.DRIFT


def test_the_threshold_is_inclusive() -> None:
    """A model reproducing its own vectors to exactly the threshold passes; an
    exclusive bound would make the documented default unreachable in practice."""
    report = classify_drift(_samples(0.95, 0.95, 0.95), threshold=0.95)

    assert report.verdict is DriftVerdict.MATCH


def test_the_report_carries_the_numbers_the_operator_has_to_read() -> None:
    report = classify_drift(_samples(0.10, 0.90, 0.50), threshold=0.95)

    assert report.median == pytest.approx(0.50)
    assert report.minimum == pytest.approx(0.10)
    assert report.maximum == pytest.approx(0.90)
    assert report.sampled == 3


def test_a_similarity_outside_the_cosine_range_is_refused() -> None:
    """A parsing bug that yields 12.0 must not be averaged into a pass."""
    with pytest.raises(ValueError, match="similarity"):
        classify_drift(_samples(0.99, 12.0), threshold=0.95)


def test_a_clean_match_exits_zero() -> None:
    report = classify_drift(_samples(0.999, 0.998), threshold=0.95)

    assert final_exit_code(report, problems=[]) == 0


def test_a_match_measured_on_a_broken_run_is_not_a_pass() -> None:
    """Measured 2026-09-21: a concurrent bench saturated the shim and one of
    the five types answered 503 for its whole batch. The remaining rows still
    produced a MATCH. A switch preflight that exits 0 there is a gate that
    passes on four fifths of a question, so an incomplete run degrades to
    UNMEASURABLE's code instead."""
    report = classify_drift(_samples(0.999, 0.998), threshold=0.95)

    assert final_exit_code(report, problems=["decision: endpoint unavailable (gpu_busy)"]) == 2


def test_proven_drift_outranks_an_incomplete_run() -> None:
    """Drift measured on the rows that DID answer is a finding, not a gap:
    reporting it as merely unmeasurable would lose the one fact that matters."""
    report = classify_drift(_samples(0.02, 0.03), threshold=0.95)

    assert final_exit_code(report, problems=["adr: endpoint unavailable (gpu_busy)"]) == 1


def test_a_drifting_sampled_type_fails_even_when_the_global_median_matches() -> None:
    """The switch gate covers every sampled type, not their weighted average.

    Six fresh types can numerically hide stale plans and plan chunks.  That
    would green-light a switch while two vector tables still use the old model.
    """
    samples = [
        SampleComparison(kind, str(i), similarity)
        for kind, similarity in [
            ("learning", 0.999),
            ("decision", 0.999),
            ("snippet", 0.999),
            ("runbook", 0.999),
            ("adr", 0.999),
            ("feature", 0.999),
            ("plan", 0.01),
            ("plan_chunk", 0.01),
        ]
        for i in range(8)
    ]

    report = classify_drift(samples, threshold=0.95)

    assert report.median is not None and report.median > 0.95
    assert report.verdict is DriftVerdict.DRIFT
    assert final_exit_code(report, problems=[]) == 1


class TestThePerTypeBreakdown:
    """A global median can hide a type that is entirely the old model's.

    The sample is spread across the covered types, so one type wholly written by
    a different model is a minority of the rows. With eight types' worth of
    matching rows around it, the median never moves and the check reports MATCH
    on a corpus a quarter of which is stale. The breakdown makes that visible
    before the verdict rule has to decide anything.
    """

    def test_each_sampled_type_is_reported_separately(self) -> None:
        samples = [
            SampleComparison(entity_type="learning", entity_id=f"l{i}", similarity=0.99)
            for i in range(8)
        ] + [
            SampleComparison(entity_type="snippet", entity_id=f"s{i}", similarity=0.21)
            for i in range(8)
        ]

        report = classify_drift(samples, threshold=0.95)

        by_type = {b.entity_type: b for b in report.by_type}
        assert set(by_type) == {"learning", "snippet"}
        assert by_type["learning"].sampled == 8
        assert by_type["snippet"].sampled == 8
        assert by_type["learning"].median == pytest.approx(0.99)
        assert by_type["snippet"].median == pytest.approx(0.21)
        assert by_type["snippet"].outliers == 8

    def test_the_global_median_alone_would_have_called_this_a_match(self) -> None:
        """The exact shape this breakdown exists to expose, stated as a number."""
        samples = [
            SampleComparison(entity_type="learning", entity_id=f"l{i}", similarity=0.99)
            for i in range(40)
        ] + [
            SampleComparison(entity_type="snippet", entity_id=f"s{i}", similarity=0.21)
            for i in range(8)
        ]

        report = classify_drift(samples, threshold=0.95)

        assert report.median is not None and report.median >= 0.95
        snippet = next(b for b in report.by_type if b.entity_type == "snippet")
        assert snippet.median is not None and snippet.median < 0.95

    def test_an_empty_sample_reports_no_types_rather_than_inventing_one(self) -> None:
        report = classify_drift([], threshold=0.95)

        assert report.by_type == ()
