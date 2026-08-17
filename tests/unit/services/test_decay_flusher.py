"""Unit tests for DecayFlusher — access_log aggregation + freshness updates.

Updated to reflect the atomic single-transaction contract introduced to fix the
non-atomic flush bug: _flush now calls aggregate_in_session(session) instead of
aggregate_and_flush(), so that the aggregate + DELETE + entity updates all live
in one transaction.  Tests that previously asserted aggregate_and_flush() was
called have been updated to assert aggregate_in_session() is called (the correct
new behaviour).  This is a legitimate test update: the old assertions encoded
the buggy two-session pattern, not the intended contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.decay import DecayCalculator
from brain_v42.services.decay_flusher import DecayFlusher


@pytest.fixture
def decay_calculator() -> DecayCalculator:
    return DecayCalculator()


@pytest.fixture
def access_log_repo() -> AsyncMock:
    repo = AsyncMock()
    # New contract: aggregate_in_session takes a session arg
    repo.aggregate_in_session = AsyncMock(return_value={})
    repo.purge_old = AsyncMock()
    return repo


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory


@pytest.fixture
def flusher(
    session_factory: MagicMock,
    access_log_repo: AsyncMock,
    decay_calculator: DecayCalculator,
) -> DecayFlusher:
    return DecayFlusher(
        session_factory=session_factory,
        access_log_repo=access_log_repo,
        decay_calculator=decay_calculator,
        interval_seconds=300,
    )


class TestFlush:
    @pytest.mark.asyncio
    async def test_plan_events_update_the_parent_plan_table(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """A plan access event targets indexed_plans, never indexed_plan_chunks."""
        plan_id = uuid4()
        accessed_at = datetime.now(tz=UTC)
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("plan", plan_id): {"max_accessed": accessed_at, "count": 1},
            }
        )
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = [
            {
                "id": plan_id,
                "created_at": accessed_at - timedelta(days=730),
                "access_count": 4,
                "access_count_human": 0,
                "freshness_status": "stale",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
            }
        ]
        session.execute = AsyncMock(side_effect=[select_result, MagicMock()])
        calculator = MagicMock()
        calculator.compute_multiplier.return_value = 0.8
        calculator.freshness_status.return_value = "fresh"
        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=calculator,
        )

        await flusher._flush()

        statements = [call.args[0] for call in session.execute.await_args_list]
        assert "indexed_plans" in str(statements[0])
        assert "indexed_plan_chunks" not in " ".join(map(str, statements))
        calculator.compute_multiplier.assert_called_once_with(
            entity_type="plan",
            created_at=accessed_at - timedelta(days=730),
            last_accessed_at=accessed_at,
            access_count=5,
            is_validated=False,
        )

    @pytest.mark.asyncio
    async def test_flush_writes_accumulated_count_human(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """new_access_count_human accumulates row + stats and lands in the UPDATE bind params.

        Regression guard for decay_flusher.py's `new_access_count_human = row[
        "access_count_human"] + stats.get("count_human", 0)` line and the
        `access_count_human=sa.bindparam("access_count_human")` wiring: a wrong
        dict key, a wrong accumulator, or a missing/swapped bindparam would all
        make this test fail, while every other existing test either omits
        "count_human" from its aggregate_in_session mock or never asserts on
        the resulting UPDATE params — so none of them would catch it.
        """
        entity_id = uuid4()
        accessed_at = datetime.now(tz=UTC)
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("learning", entity_id): {
                    "max_accessed": accessed_at,
                    "count": 2,
                    "count_human": 3,
                },
            }
        )
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = [
            {
                "id": entity_id,
                "created_at": accessed_at - timedelta(days=10),
                "access_count": 10,
                "access_count_human": 7,
                "freshness_status": "fresh",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
                "validated_at": None,
            }
        ]
        session.execute = AsyncMock(side_effect=[select_result, MagicMock()])
        calculator = MagicMock()
        calculator.compute_multiplier.return_value = 1.0
        # Same status as the mocked row ("fresh") → status_same branch (upd_same).
        calculator.freshness_status.return_value = "fresh"
        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=calculator,
        )

        await flusher._flush()

        update_call = session.execute.await_args_list[1]
        update_stmt, update_params = update_call.args[0], update_call.args[1]
        compiled = str(update_stmt.compile())
        assert "access_count_human" in compiled, (
            f"UPDATE must bind access_count_human, got: {compiled}"
        )
        assert update_params[0]["access_count_human"] == 10  # 7 (row) + 3 (count_human)
        assert update_params[0]["access_count"] == 12  # 10 (row) + 2 (count)

    @pytest.mark.asyncio
    async def test_flush_calls_aggregate(
        self, flusher: DecayFlusher, access_log_repo: AsyncMock
    ) -> None:
        """_flush calls access_log_repo.aggregate_in_session(session).

        Updated from aggregate_and_flush() → aggregate_in_session() to reflect
        the atomic flush redesign (single transaction owns both aggregation and
        entity updates).
        """
        await flusher._flush()
        access_log_repo.aggregate_in_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_calls_purge_old(
        self, flusher: DecayFlusher, access_log_repo: AsyncMock
    ) -> None:
        """_flush calls access_log_repo.purge_old(30)."""
        await flusher._flush()
        access_log_repo.purge_old.assert_called_once_with(30)

    @pytest.mark.asyncio
    async def test_flush_updates_freshness_on_changed_entities(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """_flush updates freshness_status when decay_multiplier crosses threshold."""
        entity_id = uuid4()
        now = datetime.now(tz=UTC)
        old_date = now - timedelta(days=400)

        # Mock aggregate_in_session returns one entity
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("learning", entity_id): {
                    "max_accessed": now,
                    "count": 2,
                }
            }
        )

        # Mock the batch SELECT — returns list of row mappings
        entity_row = {
            "id": entity_id,
            "created_at": old_date,
            "last_accessed_at": None,
            "last_accessed_at_human": None,
            "access_count": 0,
            "access_count_human": 0,
            "freshness_status": "fresh",
            "validated_at": None,
        }
        entity_result = MagicMock()
        entity_result.mappings.return_value.all.return_value = [entity_row]
        session.execute = AsyncMock(return_value=entity_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
            interval_seconds=300,
        )
        await flusher._flush()

        # Should have called execute for batch SELECT + UPDATE
        assert session.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_flush_noop_when_no_aggregated_entities(
        self, flusher: DecayFlusher, session_factory: MagicMock, access_log_repo: AsyncMock
    ) -> None:
        """_flush with empty aggregate doesn't query entities."""
        access_log_repo.aggregate_in_session = AsyncMock(return_value={})
        await flusher._flush()
        access_log_repo.aggregate_in_session.assert_called_once()
        access_log_repo.purge_old.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_batches_same_type_in_single_select(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """Multiple entities of the same type produce 1 SELECT + N UPDATEs."""
        id1 = uuid4()
        id2 = uuid4()
        now = datetime.now(tz=UTC)
        created = now - timedelta(days=10)

        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("decision", id1): {"max_accessed": now, "count": 1},
                ("decision", id2): {"max_accessed": now, "count": 3},
            }
        )

        rows = [
            {
                "id": id1,
                "created_at": created,
                "access_count": 0,
                "access_count_human": 0,
                "freshness_status": "fresh",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
            },
            {
                "id": id2,
                "created_at": created,
                "access_count": 5,
                "access_count_human": 0,
                "freshness_status": "fresh",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
            },
        ]
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=select_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # 1 SELECT + 1-2 bulk UPDATEs = 2-3 calls (NOT 11)
        assert session.execute.call_count <= 3

    @pytest.mark.asyncio
    async def test_flush_groups_by_entity_type(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """Entities of different types are grouped: 1 SELECT per type."""
        id_decision = uuid4()
        id_learning = uuid4()
        now = datetime.now(tz=UTC)
        created = now - timedelta(days=10)

        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("decision", id_decision): {"max_accessed": now, "count": 1},
                ("learning", id_learning): {"max_accessed": now, "count": 2},
            }
        )

        row_decision = {
            "id": id_decision,
            "created_at": created,
            "access_count": 0,
            "access_count_human": 0,
            "freshness_status": "fresh",
            "last_accessed_at": None,
            "last_accessed_at_human": None,
        }
        row_learning = {
            "id": id_learning,
            "created_at": created,
            "access_count": 0,
            "access_count_human": 0,
            "freshness_status": "fresh",
            "last_accessed_at": None,
            "last_accessed_at_human": None,
            "validated_at": None,
        }

        select_result = MagicMock()
        # Return whichever rows — both calls return one row
        select_result.mappings.return_value.all.side_effect = [
            [row_decision],
            [row_learning],
        ]
        session.execute = AsyncMock(return_value=select_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # 2 SELECTs + 1-2 bulk UPDATEs per type = <= 4 execute calls
        assert session.execute.call_count <= 4

    @pytest.mark.asyncio
    async def test_flush_skips_unknown_entity_type(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """Unknown entity_type is silently skipped."""
        entity_id = uuid4()
        now = datetime.now(tz=UTC)

        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("unknown_type", entity_id): {"max_accessed": now, "count": 1},
            }
        )

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # Session opened but no execute calls (type skipped)
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_skips_missing_entity_in_batch(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """Entity IDs not found in SELECT are silently skipped."""
        id_exists = uuid4()
        id_missing = uuid4()
        now = datetime.now(tz=UTC)
        created = now - timedelta(days=10)

        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={
                ("snippet", id_exists): {"max_accessed": now, "count": 1},
                ("snippet", id_missing): {"max_accessed": now, "count": 1},
            }
        )

        # Only id_exists comes back from SELECT
        rows = [
            {
                "id": id_exists,
                "created_at": created,
                "access_count": 0,
                "access_count_human": 0,
                "freshness_status": "fresh",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
            },
        ]
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=select_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # 1 SELECT + 1 UPDATE (missing entity skipped) = 2
        assert session.execute.call_count == 2


class TestCollectorWiring:
    """_flush must report stale/archived/access_log counts to the metrics collector."""

    @pytest.mark.asyncio
    async def test_flush_records_decay_stats_when_collector_provided(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        collector = MagicMock()
        access_log_repo.row_count = AsyncMock(return_value=42)
        access_log_repo.aggregate_in_session = AsyncMock(return_value={})
        # Any session.execute returns a scalar-yielding result
        result = MagicMock()
        result.one.return_value = (3, 2)  # (stale, archived) per table
        session.execute = AsyncMock(return_value=result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
            collector=collector,
        )
        await flusher._flush()

        collector.record_decay_stats.assert_called_once()
        kwargs = collector.record_decay_stats.call_args.kwargs
        assert kwargs["access_log_size"] == 42
        # 6 tables × (3 stale, 2 archived) = 18 stale, 12 archived
        assert kwargs["stale_count"] == 18
        assert kwargs["archived_count"] == 12

    @pytest.mark.asyncio
    async def test_flush_without_collector_does_not_record_stats(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
    ) -> None:
        """Collector is optional — flush works fine without it."""
        access_log_repo.aggregate_in_session = AsyncMock(return_value={})
        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
            collector=None,
        )
        await flusher._flush()  # must not raise


class TestBulkUpdate:
    @pytest.mark.asyncio
    async def test_update_entities_batch_fewer_calls_than_entities(
        self,
        session_factory: MagicMock,
        access_log_repo: AsyncMock,
        decay_calculator: DecayCalculator,
        session: AsyncMock,
    ) -> None:
        """_update_entities_batch must use bulk UPDATE, not one UPDATE per entity."""
        now = datetime.now(tz=UTC)
        created = now - timedelta(days=10)  # fresh entities

        # 10 entities — old code would do 1 SELECT + 10 UPDATEs = 11 calls
        ids = [uuid4() for _ in range(10)]
        aggregated = {("decision", eid): {"max_accessed": now, "count": 1} for eid in ids}
        access_log_repo.aggregate_in_session = AsyncMock(return_value=aggregated)

        rows = [
            {
                "id": eid,
                "created_at": created,
                "access_count": 0,
                "access_count_human": 0,
                "freshness_status": "fresh",
                "last_accessed_at": None,
                "last_accessed_at_human": None,
            }
            for eid in ids
        ]
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=select_result)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=decay_calculator,
        )
        await flusher._flush()

        # Bulk: 1 SELECT + 1-2 bulk UPDATEs = 2-3 calls (NOT 11)
        call_count = session.execute.call_count
        assert call_count <= 3, (
            f"Expected bulk UPDATE (<=3 calls), got {call_count} (likely N individual UPDATEs)"
        )


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self, flusher: DecayFlusher) -> None:
        """start() creates a background task."""
        await flusher.start()
        assert flusher._task is not None
        assert not flusher._task.done()
        await flusher.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, flusher: DecayFlusher) -> None:
        """stop() cancels the background task."""
        await flusher.start()
        task = flusher._task
        await flusher.stop()
        assert task is not None
        assert task.cancelled() or task.done()
