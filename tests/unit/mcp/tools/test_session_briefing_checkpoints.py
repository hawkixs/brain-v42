"""The last judgments of a session, rendered where a resume actually looks.

`SPEC-checkpoint.md` §2.4 asks `brain_session_resume` to return recent
checkpoints, and bounds them at five "to stay under the briefing ceiling". It
reasons in BRIEFING ceiling, not in structured fields — and the measurement says
it had to. A `recent_checkpoints` list on `BrainSessionResumeResult` costs 639
compact bytes of output schema; the budget in
`tests/unit/mcp/test_session_lifecycle_tool_discovery.py` had 79 left, and its own
comment pre-decided the case: "If a future field exceeds 9_541, we stop — we do
NOT loosen OUTPUT_SCHEMA_MINIMUM_SAVINGS, that would be modifying a test to make
code pass." The briefing is already a `str` in that contract, so rendering into it
costs ZERO. Same route the repository already takes for tickets, roadmap and
killswitches.

STALENESS REUSES `SESSION_STALE_AFTER` (24 h) AND INTRODUCES NO FOURTH NUMBER.
The threshold family is already populated and coherent — 4 h closes an inactive
tracer, 24 h displays `is_stale`, 7 d abandons — and B7 is a non-blocking display,
which is exactly what the 24 h means. Operator decision of 2026-09-02, taken with
the constraint §0ter.5 names: a B7 shorter than the 4 h would mark a tracer
"semantically stale" before it is closed, putting two contradictory signals on one
screen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from brain_v42.mcp.tools.session_tools import _section_checkpoints
from brain_v42.models.brain_session import (
    RESUME_CHECKPOINT_LIMIT,
    SESSION_STALE_AFTER,
    BrainSessionCheckpoint,
)


def _checkpoint(
    seq: int,
    *,
    age: timedelta = timedelta(minutes=5),
    progress: str = "shim bearer wired",
    next_step: str = "prove the trigger",
    blocker: str | None = None,
) -> BrainSessionCheckpoint:
    return BrainSessionCheckpoint(
        session_id=uuid4(),
        seq=seq,
        progress=progress,
        next_step=next_step,
        blocker=blocker,
        created_at=datetime.now(UTC) - age,
    )


class TestTheSectionSaysWhereTheWorkStood:
    def test_no_checkpoint_renders_nothing_at_all(self) -> None:
        """An empty section would teach the reader to skip the block that matters.

        Same doctrine as every other optional section here: silence when there is
        nothing to say, so presence itself carries information.
        """
        assert _section_checkpoints([]) == ""

    def test_it_renders_progress_next_step_and_seq(self) -> None:
        section = _section_checkpoints([_checkpoint(4)])

        assert "shim bearer wired" in section
        assert "prove the trigger" in section
        assert "#4" in section

    def test_a_blocker_is_rendered_and_its_absence_is_not_faked(self) -> None:
        """`blocker` is nullable, and "no blocker" must not read like an empty one."""
        with_blocker = _section_checkpoints([_checkpoint(1, blocker="embedding shim 503s")])
        without = _section_checkpoints([_checkpoint(1)])

        assert "embedding shim 503s" in with_blocker
        assert "blocked" in with_blocker.lower()
        assert "blocked" not in without.lower()

    def test_newest_first(self) -> None:
        """A resume asks "where was I", and the answer is the last judgment."""
        section = _section_checkpoints([_checkpoint(9), _checkpoint(8), _checkpoint(7)])
        positions = [section.index(f"#{seq}") for seq in (9, 8, 7)]

        assert positions == sorted(positions)

    def test_it_renders_at_most_the_bound(self) -> None:
        """The caller bounds the QUERY; this is the second half of the same promise."""
        many = [_checkpoint(seq) for seq in range(20, 0, -1)]

        section = _section_checkpoints(many)

        assert section.count("→ #") == RESUME_CHECKPOINT_LIMIT


class TestSemanticFreshnessReusesTheExistingThreshold:
    """B7 = `SESSION_STALE_AFTER`, the constant that already exists (decision 2026-09-02)."""

    def test_a_recent_checkpoint_is_not_marked_stale(self) -> None:
        section = _section_checkpoints([_checkpoint(1, age=SESSION_STALE_AFTER / 2)])

        assert "stale" not in section.lower()

    def test_a_checkpoint_older_than_the_threshold_is_marked(self) -> None:
        section = _section_checkpoints(
            [_checkpoint(1, age=SESSION_STALE_AFTER + timedelta(hours=1))]
        )

        assert "stale" in section.lower()

    def test_only_the_newest_decides_staleness(self) -> None:
        """Freshness is a property of the LAST judgment, not of the oldest one kept.

        Marking the whole block because an old checkpoint is still displayed would
        make the warning fire on every long session — the permanent alarm this
        repository has already paid for once.
        """
        section = _section_checkpoints(
            [
                _checkpoint(2, age=timedelta(minutes=1)),
                _checkpoint(1, age=SESSION_STALE_AFTER * 3),
            ]
        )

        assert "stale" not in section.lower()
