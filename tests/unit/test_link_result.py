"""Unit tests for LinkJobResult — 4-bucket accounting for idempotent graph-link jobs."""

from __future__ import annotations

import pytest

from brain_v42.services.link_result import LinkJobResult


class TestFreshnessRatio:
    """Tests for the freshness_ratio property."""

    def test_empty_result_has_zero_freshness(self):
        """Empty result → freshness_ratio is 0.0 (no division by zero)."""
        result = LinkJobResult()
        assert result.freshness_ratio == 0.0

    def test_all_created_has_freshness_one(self):
        """3 created, 0 matched → freshness_ratio == 1.0."""
        result = LinkJobResult(
            created=[{"a": 1}, {"b": 2}, {"c": 3}],
        )
        assert result.freshness_ratio == 1.0

    def test_all_matched_has_freshness_zero(self):
        """0 created, 3 matched → freshness_ratio == 0.0."""
        result = LinkJobResult(
            matched=[{"a": 1}, {"b": 2}, {"c": 3}],
        )
        assert result.freshness_ratio == 0.0

    def test_mixed_has_expected_ratio(self):
        """2 created, 3 matched → freshness_ratio == 2/5 == 0.4."""
        result = LinkJobResult(
            created=[{"a": 1}, {"b": 2}],
            matched=[{"c": 3}, {"d": 4}, {"e": 5}],
        )
        assert result.freshness_ratio == pytest.approx(0.4)


class TestExtend:
    """Tests for the extend() method."""

    def test_extend_concatenates_buckets(self):
        """extend(other) appends each bucket from other onto self."""
        a = {"id": "a"}
        b = {"id": "b"}
        c = {"id": "c"}

        r1 = LinkJobResult(created=[a])
        r2 = LinkJobResult(created=[b], matched=[c])

        r1.extend(r2)

        assert r1.created == [a, b]
        assert r1.matched == [c]
        assert r1.skipped == []
        assert r1.errors == []


class TestAsSummary:
    """Tests for the as_summary() method."""

    def test_as_summary_formats_string(self):
        """as_summary() returns a one-line string with all bucket counts and freshness."""
        result = LinkJobResult(
            created=[{"x": 1}, {"y": 2}],
            matched=[{"a": 1}, {"b": 2}, {"c": 3}],
            skipped=[{"s": 1}],
            errors=[],
        )
        summary = result.as_summary()
        assert "created=2" in summary
        assert "matched=3" in summary
        assert "skipped=1" in summary
        assert "errors=0" in summary
        assert "freshness=0.40" in summary
