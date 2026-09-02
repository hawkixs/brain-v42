"""Step 0 of `55a21fb8`: the provenance hole becomes COUNTED.

The signal already existed — `freshness_status_updated_at IS NOT NULL AND
freshness_source IS NULL` has been computable since migration 043, without a line
of code. What was missing was a READER. Measured on 2026-08-22 over the real
window (2026-08-10 → 2026-08-22, twelve days): **3 mute transitions out of 44**,
all on `learnings`, all towards `archived`, all attributable to REORG by
cross-referencing. Nobody had seen them because nobody was looking.

Two properties these tests pin, and that are step 0's contract:

* **it changes NO behaviour.** The count never escalates the script's exit. That
  is what makes it free, and what allows shipping it before the fix: if step 1
  partially fails, it is this counter that will say so, and it must already be
  there and already believed.
* **the figure carries its definition.** "3 mute" is an **UPPER bound**, not a
  count: 043's trigger documents its own blind spot — two consecutive transitions
  from the SAME source are indistinguishable from a source not redeclared, so the
  second falls back to `NULL`. A count published without that sentence would read
  as a number of guilty writers.
"""

from __future__ import annotations

import datetime as dt
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncSession

#: The query returns five columns since step 2: the last two RESTRICT the first
#: two to the `fresh` destination. This module's fixtures only speak of the
#: totals, so they leave them at zero — the direction has its own module.
_ROW_COLUMNS = ("table_name", "night", "standing", "to_fresh_night", "to_fresh_standing")


def _result(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        MagicMock(
            _mapping=dict(
                zip(_ROW_COLUMNS, (*row, *(0,) * (len(_ROW_COLUMNS) - len(row))), strict=True)
            )
        )
        for row in rows
    ]
    return result


def _report(
    rows: list[tuple], run_date: dt.date = dt.date(2026, 8, 22)
) -> post_run_alert.ProvenanceReport:
    return post_run_alert.ProvenanceReport(
        run_date=run_date,
        counts=tuple(
            post_run_alert.ProvenanceCount(table=table, night=night, standing=standing)
            for table, night, standing in rows
        ),
    )


def test_a_mute_night_names_its_tables_and_both_numbers() -> None:
    block = _report([("learnings", 3, 3), ("decisions", 0, 5)]).block

    joined = " ".join(block)
    assert post_run_alert.PROVENANCE_HEADING in block[0]
    assert "learnings 3" in joined, "la table fautive doit être nommée, pas seulement comptée"
    assert "decisions" not in joined, "une table sans transition muette n'encombre pas la ligne"


def test_the_number_never_ships_without_its_definition() -> None:
    """A bare figure would lie: this one is an UPPER BOUND, and says so."""
    joined = " ".join(_report([("learnings", 3, 3)]).block)

    assert "borne HAUTE" in joined
    assert "043" in joined, "l'angle mort doit être traçable jusqu'au trigger qui le documente"


def test_a_green_night_still_prints() -> None:
    """Silence reads "nothing to report"; a written zero reads "measured at zero".

    This is the discipline the coverage block already applies: print even when
    green, so that two nights are comparable without going to read a log.
    """
    block = _report([("learnings", 0, 0)]).block

    assert block, "une nuit sans transition muette imprime quand même son compte"
    assert "0" in " ".join(block)


def test_the_machine_line_carries_both_windows() -> None:
    """The night AND the cumulative total: two windows, two numbers, never conflated."""
    line = _report([("learnings", 3, 7), ("snippets", 0, 2)]).machine_line

    assert "mute_night=3" in line
    assert "mute_standing=9" in line
    assert "run_date=2026-08-22" in line


def test_the_count_never_escalates() -> None:
    """Step 0 changes NO behaviour — that is its whole point.

    Without this witness, a step 0 that made the script exit 2 would turn the
    night red on an observation, and the next person would disarm it.
    """
    noisy = _report([("learnings", 99, 999)])
    assert not hasattr(noisy, "escalates"), "le compte de provenance n'a PAS de verdict"

    # The exit code stays driven by COVERAGE alone. Pinned on the source because
    # it is the property a future "while we are at it" would remove in one line,
    # and that no rendering test would see fall.
    source = inspect.getsource(post_run_alert.review_and_render)
    return_lines = [line for line in source.splitlines() if line.strip().startswith("return ")]
    assert return_lines == ["    return rendered, night.coverage.escalates"], return_lines


@pytest.mark.asyncio
async def test_a_declared_transition_is_not_counted() -> None:
    """Negative witness: what declares its provenance does not count as mute.

    Without it, a query counting ALL the transitions would return 44 instead of 3
    and would read as a catastrophe.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result([("learnings", 3, 3), ("decisions", 0, 0)]))

    report = await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 22))

    assert report.night_total == 3
    assert report.standing_total == 3
    assert session.execute.await_count == 1, "une seule requête pour les six tables"


@pytest.mark.asyncio
async def test_the_statement_filters_on_both_halves_of_the_signal() -> None:
    """The signal is a CONJUNCTION, and both halves must be there.

    `freshness_source IS NULL` alone would also count the rows that never went
    through a transition since 043 — that is, almost the whole corpus.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_result([]))

    await post_run_alert.fetch_mute_transitions(session, dt.date(2026, 8, 22))

    compiled = str(session.execute.await_args.args[0].compile())
    assert "freshness_status_updated_at IS NOT NULL" in compiled
    assert "freshness_source IS NULL" in compiled
    for table in ("decisions", "learnings", "snippets", "runbooks", "adrs", "indexed_plans"):
        assert f"FROM {table}" in compiled, f"{table} doit être dans le balayage"


@pytest.mark.asyncio
async def test_the_count_is_actually_PRINTED_not_merely_computed() -> None:
    """The witness the three green, inert batches of 21-22/08 lacked.

    A counter shipped, tested, and never wired reads exactly like a counter that
    returns zero. This test follows the LIVE path: `review_night` does query the
    provenance, and `render_stdout` prints it.
    """
    session = AsyncMock(spec=AsyncSession)
    coverage_rows = MagicMock()
    coverage_rows.all.return_value = []
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    session.execute = AsyncMock(
        side_effect=[
            coverage_rows,
            coverage_rows,
            count_result,
            _result([("learnings", 3, 7)]),
        ]
    )

    rendered, escalates = await post_run_alert.review_and_render(session, dt.date(2026, 8, 22))

    assert session.execute.await_count == 4, "la lecture de provenance doit AVOIR eu lieu"
    assert escalates is False, "un compte muet n'escalade jamais"
    assert post_run_alert.PROVENANCE_HEADING in rendered
    assert "learnings 3" in rendered
    assert "mute_night=3 mute_standing=7" in rendered
    assert "borne HAUTE" in rendered


def test_an_existing_caller_without_provenance_still_renders() -> None:
    """The signature stays backward-compatible: the block disappears, nothing breaks."""
    coverage = post_run_alert.coverage_fallback(expected=1, observed=1, missing=0)

    rendered = post_run_alert.render_stdout(None, dt.date(2026, 8, 22), coverage)

    assert post_run_alert.PROVENANCE_HEADING not in rendered
    assert "dream_provenance" not in rendered
