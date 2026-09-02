"""Q1 — a ROBOT has no right to pull a record back out of the archive.

THE EXACT PATH, traced before writing a line of fix. A read — wherever it comes
from — goes through ``AccessLogger.log_access``, which writes an ``access_log``
row carrying the actor read from the request's ContextVar. Five minutes later
``DecayFlusher._flush`` aggregates those rows, and ``_update_entities_batch``
recomputes a multiplier then WRITES ``freshness_status`` (with
``freshness_source='score'``, 043). The term that tips it is ``access_factor``:
weight **0.3** on five of the six types (**0.2** for ``adr``), and a read from a
minute ago sets it to 1.0. It is NEVER dominated by age — ``w_access >= w_age``
for all six types, measured — so the common phrasing "the heaviest AFTER age"
understates its place: it is in fact the heaviest term, tied with age for
``decision``/``learning`` and with frequency for ``snippet``/``runbook``/``plan``;
only ``adr`` sees it dominated, by validation (0.5). A single machine re-read is
therefore enough to cross ``archive_threshold`` (0.2) — and, measured, to cross
``stale_threshold`` (0.5) in one go as well.

SURVEY OF THE WRITERS, through four distinct angles (kwarg, string key, raw SQL,
``freshness_source``) — there are five, and **only one** is driven by reads:

  - ``DecayFlusher._update_entities_batch``  source ``score``   <== THIS ONE
  - ``brain_refresh_entity`` (decay_tools)   source ``revive``  deliberate act
  - ``EntityMaintenanceService.refresh``     source ``revive``  deliberate act
  - ``consolidation`` (merge)                source ``merge``   goes TOWARDS the archive
  - ``pg_indexed_plan_repo`` (upsert)        (none)             re-indexing

MEASUREMENT OF 2026-08-22, replayed and not copied — the mandate announced
"~2×/day". Over 7 days of log, 31 freshness transitions, of which:

    archived -> fresh   27      <== UNARCHIVINGS, 3.86/day
    fresh    -> stale    3
    stale    -> fresh    1

Unarchivings are **87 % of all decay activity**: the dominant observable
behaviour of the decay today is undoing its own archiving. All 27 landed at
**04:00 UTC, all 27** — the dream's window. And across the 27 entities involved,
``last_accessed_at_human IS NULL`` **27 times out of 27**,
``access_count_human = 0`` **27 times out of 27**: not one has EVER been read by
a human, not once since 041.

THE MANDATE'S TRAP, AND WHY THIS MEASUREMENT ESCAPES IT. "A control is hollow as
soon as the controlled object can influence its own signal." The direction and
the hour come from the LOG, which touches no column. The attribution comes from
``last_accessed_at_human``/``access_count_human``, which **only** a human read
writes — and the measurement was taken in ``psql``, never through ``brain_get``
nor ``brain_search``, which would have written the very ``access_log`` rows it
counts. The direction of the error is favourable too: contamination would have
moved those columns away from NULL/0, hence HIDDEN the result, never
manufactured it.

WHAT THE GUARD DOES NOT DO, deliberately:
  - it does not block ENTRY into the archive — a robot keeps the right to archive;
  - it does not touch ``stale -> fresh`` — the mandate speaks of the ARCHIVE;
  - it does not freeze the counters: ``access_count`` and ``last_accessed_at``
    keep being written. They are observations, not the decision. Freezing them
    would lose real data and would stop a future human read from reopening the
    door.
  - it does NOT write ``freshness_status`` when it blocks, so it has no
    provenance to redeclare (043): the row keeps its original source.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from brain_v42.services.decay_flusher import DecayFlusher, unarchive_is_robot_only


def _session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _run_one_flush(
    *,
    old_status: str,
    computed_status: str,
    count_human: int,
) -> list[dict[str, Any]]:
    """Run ONE flush on ONE entity and return the params written.

    Returns the list of bind dictionaries passed to the UPDATEs — that is where,
    and only where, one can see whether ``freshness_status`` was written.
    """
    entity_id = uuid4()
    now = datetime.now(tz=UTC)

    repo = AsyncMock()
    repo.aggregate_in_session = AsyncMock(
        return_value={
            ("learning", entity_id): {
                "max_accessed": now,
                "max_accessed_human": now if count_human else None,
                "count": 3,
                "count_human": count_human,
            }
        }
    )
    repo.purge_old = AsyncMock()

    select_result = MagicMock()
    select_result.mappings.return_value.all.return_value = [
        {
            "id": entity_id,
            "created_at": now - timedelta(days=400),
            "access_count": 90,
            "access_count_human": 0,
            "freshness_status": old_status,
            "last_accessed_at": now - timedelta(days=200),
            "last_accessed_at_human": None,
            "validated_at": None,
        }
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[select_result, MagicMock(), MagicMock()])

    calculator = MagicMock()
    calculator.compute_multiplier.return_value = 0.9
    calculator.freshness_status.return_value = computed_status

    flusher = DecayFlusher(
        session_factory=_session_factory(session),
        access_log_repo=repo,
        decay_calculator=calculator,
    )
    asyncio.run(flusher._flush())

    written: list[dict[str, Any]] = []
    for call in session.execute.await_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], list):
            written.extend(call.args[1])
    return written


class TestTheGuardItself:
    """The decision, isolated from the flusher. Control mutation in BOTH DIRECTIONS."""

    def test_a_robot_lifting_out_of_the_archive_is_refused(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=0)
            is True
        )

    def test_one_human_read_in_the_batch_is_enough_to_allow_it(self) -> None:
        """NEGATIVE WITNESS. Without it, blocking EVERYTHING would turn the test green."""
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=1)
            is False
        )

    def test_it_says_nothing_about_going_INTO_the_archive(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="fresh", new_status="archived", human_reads=0)
            is False
        )

    def test_it_says_nothing_about_stale_to_fresh(self) -> None:
        """The mandate speaks of the ARCHIVE. A wider guard would overreach."""
        assert (
            unarchive_is_robot_only(old_status="stale", new_status="fresh", human_reads=0) is False
        )

    def test_an_archived_row_that_stays_archived_is_not_a_lift(self) -> None:
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="archived", human_reads=0)
            is False
        )


class TestThroughTheFlusher:
    """The same contract, but through the real write path."""

    def test_a_robot_read_does_not_lift_it_out_of_the_archive(self) -> None:
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=0)

        assert written, "le flush doit tout de même écrire les compteurs"
        assert all("freshness_status" not in params for params in written), (
            "une relecture ROBOT a réécrit freshness_status : la boucle n'est pas fermée"
        )

    def test_a_human_read_lifts_it_out_every_time(self) -> None:
        """NEGATIVE WITNESS, inside the test itself.

        Without this assertion, a fix blocking EVERY unarchiving — the legitimate
        case included — would leave the previous test green. That is exactly the
        failure the mandate asks to rule out.
        """
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=1)

        statuses = [p["freshness_status"] for p in written if "freshness_status" in p]
        assert statuses == ["fresh"], f"une lecture HUMAINE doit désarchiver, écrit={written}"

    def test_a_robot_may_still_send_an_entity_INTO_the_archive(self) -> None:
        written = _run_one_flush(old_status="fresh", computed_status="archived", count_human=0)

        statuses = [p["freshness_status"] for p in written if "freshness_status" in p]
        assert statuses == ["archived"], "la garde ne doit pas empêcher d'archiver"

    def test_the_counters_keep_flowing_even_when_the_lift_is_refused(self) -> None:
        """Block the DECISION, not the OBSERVATION.

        Freezing the counters would lose real data and would stop a later human
        read from legitimately reopening the door.
        """
        written = _run_one_flush(old_status="archived", computed_status="fresh", count_human=0)

        assert len(written) == 1
        params = written[0]
        assert params["access_count"] == 93
        assert params["last_accessed_at"] is not None
