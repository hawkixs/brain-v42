"""Tests for StatusEngine — pure logic, no I/O."""

import pytest

from brain_v42.services.status_engine import StatusEngine

engine = StatusEngine()


@pytest.mark.parametrize(
    "current_status, signal_type, pinned, expected",
    [
        # Basic progression
        ("planned", "learning", False, "research"),
        ("planned", "decision", False, "research"),
        ("research", "plan", False, "design"),
        ("design", "mr_opened", False, "building"),
        ("building", "mr_merged", False, "deployed"),
        # Monotonic: never go backward
        ("deployed", "learning", False, "deployed"),
        ("building", "decision", False, "building"),
        ("design", "learning", False, "design"),
        # Pinned: no change
        ("research", "mr_merged", True, "research"),
        ("planned", "plan", True, "planned"),
        # No-op signals
        ("planned", "push", False, "planned"),
        ("building", "pipeline_failure", False, "building"),
        # Snippet and runbook/adr mapping
        ("planned", "snippet", False, "research"),
        ("planned", "runbook", False, "design"),
        ("planned", "adr", False, "design"),
        # Pipeline success
        ("building", "pipeline_success", False, "deployed"),
    ],
)
def test_compute_status(current_status, signal_type, pinned, expected):
    result = engine.compute_status(current_status, signal_type, pinned)
    assert result == expected


def test_status_order_complete():
    """All valid statuses must be in STATUS_ORDER."""
    assert StatusEngine.STATUS_ORDER == [
        "planned",
        "research",
        "design",
        "building",
        "deployed",
        "done",
    ]


# ── archived / out-of-scale statuses ────────────────────────────────────


@pytest.mark.parametrize(
    "signal_type",
    ["learning", "decision", "mr_merged", "pipeline_success", "snippet", "adr"],
)
def test_compute_status_archived_is_immutable(signal_type: str) -> None:
    """archived is outside STATUS_ORDER — must be returned unchanged regardless of signal.

    Post-purge clusters can be archived; any incoming signal that lands on
    them via stale ClusterGuard candidates must not crash with ValueError.
    """
    result = engine.compute_status("archived", signal_type, pinned=False)
    assert result == "archived"


def test_compute_status_unknown_status_returned_unchanged() -> None:
    """Any status not in STATUS_ORDER is returned as-is (defensive, no ValueError)."""
    result = engine.compute_status("legacy", "learning", pinned=False)
    assert result == "legacy"
