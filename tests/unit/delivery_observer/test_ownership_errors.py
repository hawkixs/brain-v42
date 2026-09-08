"""Unwrapped driver faults must fence publication and never escape cleanup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg import InternalClientError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from brain_v42.delivery_observer.ownership import ObserverOwnership, ObserverOwnershipLost

_PRIVATE_ERROR = "cannot switch protocol state; private connection detail"


def _owned() -> tuple[ObserverOwnership, MagicMock]:
    connection = MagicMock(spec=AsyncConnection)
    connection.invalidated = False
    connection.closed = False
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    owner = ObserverOwnership(MagicMock(spec=AsyncEngine))
    owner._connection = connection
    owner._backend_pid = 42
    owner._state = "owned"
    return owner, connection


@asynccontextmanager
async def _broken_begin() -> AsyncIterator[None]:
    raise InternalClientError(_PRIVATE_ERROR)
    yield  # pragma: no cover


async def test_raw_driver_fault_during_acquire_permanently_loses_authority() -> None:
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(side_effect=InternalClientError(_PRIVATE_ERROR))
    owner = ObserverOwnership(engine)

    with pytest.raises(ObserverOwnershipLost, match="^delivery_observer_ownership_lost$"):
        await owner.acquire()

    assert owner.lost.is_set() and not owner.owned
    with pytest.raises(ObserverOwnershipLost):
        await owner.acquire()
    engine.connect.assert_awaited_once()


async def test_raw_driver_fault_during_verification_fences_the_owner() -> None:
    owner, connection = _owned()
    connection.execute = AsyncMock(side_effect=InternalClientError(_PRIVATE_ERROR))

    with pytest.raises(ObserverOwnershipLost, match="^delivery_observer_ownership_lost$"):
        await owner._verify(connection)

    assert owner.lost.is_set() and not owner.owned
    with pytest.raises(ObserverOwnershipLost):
        await owner.heartbeat()
    connection.execute.assert_awaited_once()


async def test_raw_driver_fault_before_transaction_never_yields_a_publisher() -> None:
    owner, connection = _owned()
    connection.begin.return_value = _broken_begin()

    with pytest.raises(ObserverOwnershipLost, match="^delivery_observer_ownership_lost$"):
        async with owner.transaction():
            pytest.fail("a failed physical connection yielded publication authority")

    assert owner.lost.is_set() and not owner.owned
    connection.execute.assert_not_called()


async def test_release_closes_the_backend_even_when_the_driver_is_already_broken() -> None:
    owner, connection = _owned()
    connection.begin.return_value = _broken_begin()

    await owner.release()
    await owner.release()

    assert not owner.owned and owner._connection is None
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
    with pytest.raises(ObserverOwnershipLost):
        await owner.acquire()


async def test_dispose_attempts_both_steps_without_exposing_driver_errors() -> None:
    _, connection = _owned()
    connection.invalidate.side_effect = InternalClientError(_PRIVATE_ERROR)
    connection.close.side_effect = InternalClientError(_PRIVATE_ERROR)

    await ObserverOwnership._dispose(connection)

    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
