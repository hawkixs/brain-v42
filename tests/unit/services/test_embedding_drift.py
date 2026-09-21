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
