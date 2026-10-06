"""Tests for the automation-owned feature deduplication loop."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.automation.ownership import OwnershipLostError


def test_dedup_loop_is_exposed_from_automation_context() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    assert run_dedup_loop is not None


def _session_factory() -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = [("brain-v42",)]
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _candidate(name: str) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    return row


async def test_dedup_waits_before_first_pass_and_checks_ownership_around_the_scan() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock(spec=["find_candidates"])
    job.find_candidates = AsyncMock(return_value=[(target, source, 0.91)])
    factory, _session = _session_factory()
    ownership = MagicMock()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with patch("brain_v42.automation.dedup.asyncio.sleep", sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_dedup_loop(job, factory, interval=17.0, ownership=ownership)

    assert sleep.await_args_list[0].args == (17.0,)
    assert ownership.ensure_owned.call_count == 2
    job.find_candidates.assert_awaited_once_with("brain-v42")


async def test_ownership_loss_after_candidate_scan_closes_admission_gate_before_any_signal() -> (
    None
):
    from brain_v42.automation.dedup import run_dedup_loop

    candidate_scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    target = _candidate("target")
    source = _candidate("source")

    async def find_candidates(_project_key: str) -> list[tuple[object, object, float]]:
        candidate_scan_started.set()
        await release_scan.wait()
        return [(target, source, 0.92)]

    job = MagicMock(spec=["find_candidates"])
    job.find_candidates = AsyncMock(side_effect=find_candidates)
    factory, _session = _session_factory()
    owned = True

    def ensure_owned() -> None:
        if not owned:
            raise OwnershipLostError("lease lost at mutation barrier")

    ownership = MagicMock()
    ownership.ensure_owned.side_effect = ensure_owned

    with patch("brain_v42.automation.dedup.logger") as logger:
        task = asyncio.create_task(run_dedup_loop(job, factory, interval=0.0, ownership=ownership))
        try:
            try:
                await asyncio.wait_for(candidate_scan_started.wait(), timeout=0.2)
                scan_started = True
            except TimeoutError:
                scan_started = False
            assert scan_started, "the scheduler must enter the candidate scan after its delay"
            owned = False
            release_scan.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert isinstance(result, OwnershipLostError)
            assert "mutation barrier" in str(result)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert ownership.ensure_owned.call_count == 2
    assert not [
        c for c in logger.info.call_args_list if c.args[0] == "dedup_loop.probable_duplicate"
    ]


async def test_ownership_loss_before_pass_terminates_instead_of_being_logged_and_swallowed() -> (
    None
):
    from brain_v42.automation.dedup import run_dedup_loop

    job = MagicMock()
    job.find_candidates = AsyncMock()
    factory, _session = _session_factory()
    ownership = MagicMock()
    ownership.ensure_owned.side_effect = OwnershipLostError("lease lost")

    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
    ):
        result = (
            await asyncio.gather(
                run_dedup_loop(job, factory, interval=17.0, ownership=ownership),
                return_exceptions=True,
            )
        )[0]

    assert isinstance(result, OwnershipLostError)
    assert "lease lost" in str(result)
    job.find_candidates.assert_not_awaited()
    logger.exception.assert_not_called()


async def test_non_ownership_error_is_logged_and_next_pass_remains_scheduled() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    job = MagicMock()
    job.find_candidates = AsyncMock(side_effect=RuntimeError("temporary failure"))
    factory, _session = _session_factory()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_dedup_loop(job, factory, interval=17.0)

    logger.exception.assert_called_once_with(
        "dedup_loop.project_error",
        project_key="brain-v42",
        error_type="RuntimeError",
        exc_info=True,
    )


async def test_project_scan_error_is_logged_and_next_project_continues() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock(spec=["find_candidates"])
    job.find_candidates = AsyncMock(
        side_effect=[RuntimeError("project scan failed"), [(target, source, 0.9)]]
    )

    factory, session = _session_factory()
    project_rows = MagicMock()
    project_rows.fetchall.return_value = [("broken",), ("healthy",)]
    session.execute = AsyncMock(return_value=project_rows)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_dedup_loop(job, factory, interval=17.0)

    assert job.find_candidates.await_args_list[0].args == ("broken",)
    assert job.find_candidates.await_args_list[1].args == ("healthy",)
    logger.exception.assert_called_once_with(
        "dedup_loop.project_error",
        project_key="broken",
        error_type="RuntimeError",
        exc_info=True,
    )
    signals = [
        c for c in logger.info.call_args_list if c.args[0] == "dedup_loop.probable_duplicate"
    ]
    assert len(signals) == 1
    assert signals[0].kwargs["project_key"] == "healthy"


async def test_probable_duplicates_are_signalled_and_never_merged() -> None:
    """Ruling 9e21964f: a reranker score only signals, it never reaches a write path."""
    from brain_v42.automation.dedup import run_dedup_loop

    target_a = _candidate("target-a")
    source_a = _candidate("source-a")
    target_b = _candidate("target-b")
    source_b = _candidate("source-b")
    # `spec` makes any access to a merge entry point raise AttributeError.
    job = MagicMock(spec=["find_candidates"])
    job.find_candidates = AsyncMock(
        return_value=[(target_a, source_a, 0.94), (target_b, source_b, 0.91)]
    )
    factory, session = _session_factory()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_dedup_loop(job, factory, interval=17.0)

    # One session only: the project-keys read. No write session is ever opened.
    assert factory.call_count == 1
    session.commit.assert_not_awaited()
    logger.exception.assert_not_called()
    signals = [
        c for c in logger.info.call_args_list if c.args[0] == "dedup_loop.probable_duplicate"
    ]
    assert [c.kwargs for c in signals] == [
        {
            "project_key": "brain-v42",
            "target_id": str(target_a.id),
            "target": "target-a",
            "source_id": str(source_a.id),
            "source": "source-a",
            "score": 0.94,
        },
        {
            "project_key": "brain-v42",
            "target_id": str(target_b.id),
            "target": "target-b",
            "source_id": str(source_b.id),
            "source": "source-b",
            "score": 0.91,
        },
    ]
