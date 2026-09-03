"""`access:` in a summary row counts HUMAN reads, not machine ones.

Ticket `1597c36d`, arbitrated 2026-09-03 after `REORG` archived nothing for
twelve nights. Its prompt rejects a candidate whose summary shows `access:N > 5`
-- "entity is being read, someone is using it" -- and the row was rendering
`access_count`, the MACHINE counter, which the nightly scans themselves inflate.

Measured before the change, on the 34 live entities matching REORG's trash
allowlist:

    blocked by the machine counter while NEVER read by a human ....... 18
    would pass on the human counter ................................. 33
    protected on the human counter (human > 5) ....................... 1

This repository had already settled the same question elsewhere: migration 041
added `access_count_human` and 044 `last_accessed_at_human`, precisely because
`access_factor` was driven by machine reads. The summary row was the last place
still asking the old counter.

The threshold does NOT move and no prompt changes: only which number the row
carries. The machine count stays available under `reads:`, so a phase that has a
use for it can still see it -- and so that this change is legible in the row
itself rather than only in a commit message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from brain_v42.mcp.tools.formatters import (
    _format_decision_summary,
    _format_learning_summary,
)
from brain_v42.models.decision import Decision
from brain_v42.models.learning import Learning


def _learning(**overrides: object) -> Learning:
    fields: dict[str, object] = {
        "id": uuid4(),
        "topic": "infra_status_check",
        "insight": "snapshot",
        "created_at": datetime(2026, 9, 3, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 3, tzinfo=UTC),
    }
    fields.update(overrides)
    return Learning(**fields)  # type: ignore[arg-type]


def _decision(**overrides: object) -> Decision:
    fields: dict[str, object] = {
        "id": uuid4(),
        "title": "test_phase2",
        "description": "d",
        "reasoning": "r",
        "status": "active",
        "created_at": datetime(2026, 9, 3, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 3, tzinfo=UTC),
    }
    fields.update(overrides)
    return Decision(**fields)  # type: ignore[arg-type]


class TestTheRowCountsHumanReads:
    def test_a_row_read_only_by_machines_shows_zero(self) -> None:
        """The real shape of the block: machine 101, human 0, nobody using it."""
        row = _format_learning_summary(_learning(access_count=101, access_count_human=0), 1)
        assert "access:0" in row

    def test_a_row_read_by_humans_shows_that_count(self) -> None:
        row = _format_learning_summary(_learning(access_count=3, access_count_human=7), 1)
        assert "access:7" in row

    def test_the_machine_count_stays_visible_under_reads(self) -> None:
        """Removed from `access:` is not removed from the row."""
        row = _format_learning_summary(_learning(access_count=101, access_count_human=0), 1)
        assert "reads:101" in row

    def test_a_decision_row_follows_the_same_rule(self) -> None:
        row = _format_decision_summary(_decision(access_count=63, access_count_human=2), 1)
        assert "access:2" in row
        assert "reads:63" in row


class TestTheSignIsRight:
    """A guardrail read backwards would archive what people actually use."""

    def test_an_entity_above_the_threshold_on_HUMAN_reads_still_shows_it(self) -> None:
        """`count_events_24h`: human 6, machine 10 -- it must stay protected."""
        row = _format_learning_summary(
            _learning(topic="count_events_24h", access_count=10, access_count_human=6), 1
        )
        assert "access:6" in row, "6 > 5, so REORG must still reject it"

    def test_an_absent_human_counter_reads_as_zero_and_not_as_the_machine_one(self) -> None:
        """ "Never counted" is not "counted a lot", and a guard must not fall back.

        Asserted on the helper rather than through the model, deliberately:
        `access_count_human` is typed `int = 0`, so a `Learning` can never carry
        `None`. The object that can is a raw row or a DTO built elsewhere, which
        is exactly what this guards -- falling back to the machine count on a
        missing field would silently restore the bug being fixed.
        """
        from brain_v42.mcp.tools.formatters import _summary_access

        class _WithoutTheField:
            access_count = 99

        rendered = _summary_access(_WithoutTheField())
        assert rendered.startswith("access:0")
        assert "reads:99" in rendered
