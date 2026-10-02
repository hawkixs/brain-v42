"""The extraction accounting vocabulary is closed, bounded and mirrored to retained logs."""

from __future__ import annotations

from typing import get_args

from structlog.testing import capture_logs

from brain_v42.services import claim_extraction_counters as counters


def test_counters_count_and_log_only_fixed_labels() -> None:
    """Each skip/failure raises exactly one fixed-label counter and emits the same code to the log."""
    before = counters.snapshot()
    with capture_logs() as logs:
        counters.record_skip("adr", "quota_full", entity_id="entry-1")
        counters.record_failure("snippet", "parser_error", entity_id="entry-2")
    after = counters.snapshot()

    assert after.skipped[("adr", "quota_full")] == before.skipped[("adr", "quota_full")] + 1
    assert (
        after.failed[("snippet", "parser_error")] == before.failed[("snippet", "parser_error")] + 1
    )
    assert sum(after.skipped.values()) == sum(before.skipped.values()) + 1
    assert sum(after.failed.values()) == sum(before.failed.values()) + 1
    assert [
        (log["event"], log["entity_type"], log["reason"], log["entity_id"]) for log in logs
    ] == [
        ("claim_extraction_skip", "adr", "quota_full", "entry-1"),
        ("claim_extraction_failed", "snippet", "parser_error", "entry-2"),
    ]


def test_snapshot_key_space_is_the_closed_vocabulary_and_nothing_else() -> None:
    """The snapshot enumerates every label pair once, so cardinality is fixed at design time."""
    snap = counters.snapshot()
    entities = set(get_args(counters.EntityType))
    assert set(snap.skipped) == {
        (entity, reason) for entity in entities for reason in get_args(counters.SkipReason)
    }
    assert set(snap.failed) == {
        (entity, reason) for entity in entities for reason in get_args(counters.FailureReason)
    }
    assert entities == {"learning", "decision", "adr", "snippet", "runbook"}
