"""Step 2 of `55a21fb8`: a mute transition finally states its DIRECTION.

Step 1 dried up the KNOWN source — REORG declares `judgment` — and left the human
write mute **on purpose**, so that it stays counted by step 0 and the next
unlisted source becomes visible. This module does not undo that choice: it adds
the one thing that was missing for an unarchival to stop being invisible, and
that costs no migration.

**The counter never read `freshness_status`.** Its signal is the conjunction "a
transition happened" AND "its provenance is absent". An archival and an
UNARCHIVAL therefore produced exactly the same line. An operator reading
"learnings 3" could not tell whether three entities had left the corpus or
re-entered it — whereas these are two opposite incidents, one that loses
knowledge and one that resurrects it.

Measured on production on 2026-08-23, head `046`: **44 dated transitions since
043**, of which **3 mute**, all on `learnings`, all towards `archived`. No mute
return to `fresh` to date — so this module does not describe an incident in
progress, it makes visible the one that has not yet happened. The 31 declared
transitions are all declared by `score`; `revive`, `merge` and `judgment` have
NEVER been written in production.

**What the destination says, and what it does not.** For a mute row, the current
status IS the destination of its last transition — a later transition that had
declared a source would take it out of the count. The destination is therefore
exact. But `fresh` is a SUPER-SET of unarchival: a `stale → fresh` enters it too.
Telling the two apart would require the PREVIOUS status, which nobody stores —
that is, a column, hence a migration. The number is named "returns to `fresh`",
never "unarchivals".
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession

_ROW_COLUMNS = ("table_name", "night", "standing", "to_fresh_night", "to_fresh_standing")


def _result_rows(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        MagicMock(_mapping=dict(zip(_ROW_COLUMNS, row, strict=True))) for row in rows
    ]
    return result


def _count(table: str, night: int, standing: int, to_fresh: tuple[int, int] = (0, 0)):
    return post_run_alert.ProvenanceCount(
        table=table,
        night=night,
        standing=standing,
        to_fresh_night=to_fresh[0],
        to_fresh_standing=to_fresh[1],
    )


def _report(counts):
    return post_run_alert.ProvenanceReport(run_date=dt.date(2026, 8, 23), counts=tuple(counts))


def test_a_mute_return_to_fresh_is_named_in_the_block() -> None:
    """The incident step 0 could see without being able to name it."""
    block = _report(
        [
            _count("learnings", night=2, standing=5, to_fresh=(2, 3)),
            _count("decisions", night=0, standing=1),
        ]
    ).block
    joined = " ".join(block)

    assert "fresh" in joined, "la destination n'apparaît pas"
    assert "2" in joined and "3" in joined


def test_an_archival_only_night_does_not_claim_a_return_to_fresh() -> None:
    """The negative witness: without it, the line could be printed hard-coded."""
    report = _report([_count("learnings", night=3, standing=3)])

    assert report.to_fresh_night_total == 0
    assert report.to_fresh_standing_total == 0
    assert "désarchivage" not in " ".join(report.block).lower()


def test_the_totals_of_the_first_march_are_untouched() -> None:
    """Step 2 ADDS a dimension, it replaces none."""
    report = _report(
        [
            _count("learnings", night=3, standing=7, to_fresh=(1, 2)),
            _count("snippets", night=0, standing=2),
        ]
    )

    assert report.night_total == 3
    assert report.standing_total == 9
    assert "mute_night=3" in report.machine_line
    assert "mute_standing=9" in report.machine_line


def test_the_machine_line_carries_the_direction_too() -> None:
    """A signal only a human can read enters no dashboard."""
    line = _report(
        [
            _count("learnings", night=2, standing=4, to_fresh=(2, 3)),
            _count("adrs", night=1, standing=1),
        ]
    ).machine_line

    assert "mute_to_fresh_night=2" in line
    assert "mute_to_fresh_standing=3" in line


def test_a_return_to_fresh_can_never_exceed_the_mute_count_it_refines() -> None:
    """The bound that makes the number readable: it is a SUB-set."""
    report = _report(
        [
            _count("learnings", night=2, standing=5, to_fresh=(2, 3)),
            _count("decisions", night=1, standing=1, to_fresh=(0, 1)),
        ]
    )

    assert report.to_fresh_night_total <= report.night_total
    assert report.to_fresh_standing_total <= report.standing_total


def test_the_number_is_never_called_a_desarchivage() -> None:
    """`stale → fresh` enters the same count; the word would be stronger than the measure.

    The PREVIOUS status is stored nowhere. Calling it "unarchival" would require a
    column, hence a migration.
    """
    block = " ".join(_report([_count("learnings", night=2, standing=2, to_fresh=(2, 2))]).block)

    assert "fresh" in block
    assert "désarchivage" not in block.lower()
    assert "unarchive" not in block.lower()


@pytest.mark.asyncio
async def test_the_statement_reads_the_status_it_used_to_ignore() -> None:
    """The counter NEVER read `freshness_status` — that is the whole hole.

    Pinned on the COMPILED SQL and not on the module's text: a test that reads the
    source proves something was written, never that it was sent.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result_rows([]))

    await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 23))

    statement = session.execute.await_args.args[0].compile()
    compiled = str(statement)
    # Both halves of step 0's signal SURVIVE the restriction.
    assert "freshness_status_updated_at IS NOT NULL" in compiled
    assert "freshness_source IS NULL" in compiled
    # And the destination, which step 0 did not query.
    assert "freshness_status =" in compiled, "la requête n'interroge pas la destination"
    # The compared VALUE, read from the bound parameters: `'fresh'` does not
    # appear in the rendered SQL, it is a bindparam there.
    assert post_run_alert.RETURNED_STATUS in statement.params.values()
    assert session.execute.await_count == 1, "toujours UNE seule requête pour les six tables"


def test_the_destination_compared_is_the_one_that_means_a_return() -> None:
    """The hole the control mutation revealed, closed.

    Measured on 2026-08-23: flipping `RETURNED_STATUS` to `"archived"` left the
    eight unit tests GREEN and only the real-database witness reddened. The cause
    was that the assertions compared against the CONSTANT — which followed the
    mutation. A constant cannot be its own witness.

    The value is also checked to BELONG to the model's closed vocabulary: freezing
    it without that would make it silently wrong if the vocabulary changed.
    """
    from typing import get_args

    from brain_v42.models.learning import LearningUpdate

    statuses = get_args(get_args(LearningUpdate.model_fields["freshness_status"].annotation)[0])

    assert post_run_alert.RETURNED_STATUS == "fresh"
    assert post_run_alert.RETURNED_STATUS in statuses
    assert set(statuses) == {"fresh", "stale", "archived"}


@pytest.mark.asyncio
async def test_the_two_new_columns_reach_the_report() -> None:
    """A counter computed and not read reads exactly like a counter at zero."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result_rows([("learnings", 4, 9, 3, 5)]))

    report = await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 23))

    assert report.night_total == 4
    assert report.standing_total == 9
    assert report.to_fresh_night_total == 3
    assert report.to_fresh_standing_total == 5
