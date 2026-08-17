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


async def test_dedup_waits_before_first_pass_and_preserves_merge_semantics() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[(target, source, 0.91)])
    job.merge_features = AsyncMock(return_value=True)
    factory, session = _session_factory()
    ownership = MagicMock()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with patch("brain_v42.automation.dedup.asyncio.sleep", sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_dedup_loop(job, factory, interval=17.0, ownership=ownership)

    assert sleep.await_args_list[0].args == (17.0,)
    assert ownership.ensure_owned.call_count == 6
    job.find_candidates.assert_awaited_once_with("brain-v42")
    job.merge_features.assert_awaited_once_with(session, target, source)
    session.commit.assert_awaited_once_with()


async def test_ownership_loss_between_candidate_scan_and_merge_closes_admission_gate() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    candidate_scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    target = _candidate("target")
    source = _candidate("source")

    async def find_candidates(_project_key: str) -> list[tuple[object, object, float]]:
        candidate_scan_started.set()
        await release_scan.wait()
        return [(target, source, 0.92)]

    job = MagicMock()
    job.find_candidates = AsyncMock(side_effect=find_candidates)
    job.merge_features = AsyncMock(return_value=True)
    factory, session = _session_factory()
    owned = True

    def ensure_owned() -> None:
        if not owned:
            raise OwnershipLostError("lease lost at mutation barrier")

    ownership = MagicMock()
    ownership.ensure_owned.side_effect = ensure_owned

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
    job.merge_features.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_ownership_loss_during_merge_session_checkout_is_rechecked_inside_context() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[(target, source, 0.93)])
    job.merge_features = AsyncMock(return_value=True)
    keys_session = AsyncMock()
    keys_result = MagicMock()
    keys_result.fetchall.return_value = [("brain-v42",)]
    keys_session.execute = AsyncMock(return_value=keys_result)
    merge_session = AsyncMock()
    checkout_started = asyncio.Event()
    checkout_may_finish = asyncio.Event()

    async def enter_merge_session() -> AsyncMock:
        checkout_started.set()
        await checkout_may_finish.wait()
        return merge_session

    keys_context = MagicMock()
    keys_context.__aenter__ = AsyncMock(return_value=keys_session)
    keys_context.__aexit__ = AsyncMock(return_value=False)
    merge_context = MagicMock()
    merge_context.__aenter__ = AsyncMock(side_effect=enter_merge_session)
    merge_context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(side_effect=[keys_context, merge_context])
    owned = True

    def ensure_owned() -> None:
        if not owned:
            raise OwnershipLostError("lost during session checkout")

    ownership = MagicMock()
    ownership.ensure_owned.side_effect = ensure_owned
    task = asyncio.create_task(run_dedup_loop(job, factory, interval=0.0, ownership=ownership))
    try:
        try:
            await asyncio.wait_for(checkout_started.wait(), timeout=0.2)
            checkout_observed = True
        except TimeoutError:
            checkout_observed = False
        assert checkout_observed, "merge session checkout must begin"
        owned = False
        checkout_may_finish.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            finished = True
        except TimeoutError:
            finished = False
        except OwnershipLostError:
            finished = True
        assert finished, "loss during checkout must terminate at the in-context admission gate"
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, OwnershipLostError)
        assert "session checkout" in str(result)
    finally:
        checkout_may_finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    job.merge_features.assert_not_awaited()
    merge_session.commit.assert_not_awaited()


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


async def test_candidate_error_is_logged_and_remaining_candidates_continue() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target_a = _candidate("target-a")
    source_a = _candidate("source-a")
    target_b = _candidate("target-b")
    source_b = _candidate("source-b")
    job = MagicMock()
    job.find_candidates = AsyncMock(
        return_value=[(target_a, source_a, 0.94), (target_b, source_b, 0.91)]
    )
    job.merge_features = AsyncMock(side_effect=[RuntimeError("first merge failed"), True])
    factory, session = _session_factory()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_dedup_loop(job, factory, interval=17.0)

    assert job.merge_features.await_count == 2
    session.commit.assert_awaited_once_with()
    logger.exception.assert_called_once_with(
        "dedup_loop.candidate_error",
        project_key="brain-v42",
        target="target-a",
        source="source-a",
        score=0.94,
        error_type="RuntimeError",
        exc_info=True,
    )


async def test_project_scan_error_is_logged_and_next_project_continues() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock()
    job.find_candidates = AsyncMock(
        side_effect=[RuntimeError("project scan failed"), [(target, source, 0.9)]]
    )
    job.merge_features = AsyncMock(return_value=True)

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
    job.merge_features.assert_awaited_once_with(session, target, source)
    session.commit.assert_awaited_once_with()
    logger.exception.assert_called_once_with(
        "dedup_loop.project_error",
        project_key="broken",
        error_type="RuntimeError",
        exc_info=True,
    )
