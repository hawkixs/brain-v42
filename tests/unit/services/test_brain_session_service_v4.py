"""RED service contracts for Brain session lifecycle v4."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import BrainSessionInputError
from brain_v42.services.brain_session_service import BrainSessionService


def _repo() -> MagicMock:
    repo = MagicMock()
    for method in ("resume", "capture", "heartbeat", "end", "abandon"):
        setattr(repo, method, AsyncMock(return_value=object()))
    return repo


async def test_identity_guard_is_normalized_on_every_targeted_operation() -> None:
    repo = _repo()
    service = BrainSessionService(repo)
    session_id = uuid4()
    knowledge_id = uuid4()

    await service.resume(session_id, " task-a ")
    await service.capture(session_id, " task-a ", [knowledge_id])
    await service.heartbeat(session_id, " task-a ")
    await service.end(
        session_id,
        " task-a ",
        " done ",
        " next ",
        3,
        nothing_to_capture_reason=" no durable artifact ",
    )
    await service.abandon(session_id, " task-a ", " superseded ")

    repo.resume.assert_awaited_once_with(session_id, "task-a")
    repo.capture.assert_awaited_once_with(session_id, "task-a", [knowledge_id])
    repo.heartbeat.assert_awaited_once_with(session_id, "task-a")
    repo.end.assert_awaited_once_with(
        session_id,
        "task-a",
        "done",
        "next",
        3,
        nothing_to_capture_reason="no durable artifact",
    )
    repo.abandon.assert_awaited_once_with(session_id, "task-a", "superseded")


@pytest.mark.parametrize("client_key", ["", "   ", "x" * 129])
async def test_identity_guard_rejects_invalid_client_key(client_key: str) -> None:
    repo = _repo()
    service = BrainSessionService(repo)

    with pytest.raises(BrainSessionInputError, match="expected_client_key"):
        await service.heartbeat(uuid4(), client_key)

    repo.heartbeat.assert_not_awaited()


async def test_capture_requires_one_to_one_hundred_unique_ids() -> None:
    repo = _repo()
    service = BrainSessionService(repo)
    duplicate = uuid4()

    for invalid in ([], [duplicate, duplicate], [uuid4() for _ in range(101)]):
        with pytest.raises(BrainSessionInputError):
            await service.capture(uuid4(), "task-a", invalid)

    repo.capture.assert_not_awaited()


async def test_end_allows_repository_to_resolve_attached_capture_outcome() -> None:
    repo = _repo()
    service = BrainSessionService(repo)
    session_id = uuid4()

    await service.end(session_id, "task-a", "done", "proposal", 4)

    repo.end.assert_awaited_once_with(
        session_id,
        "task-a",
        "done",
        "proposal",
        4,
        nothing_to_capture_reason=None,
    )


async def test_list_accepts_stale_as_a_derived_filter() -> None:
    repo = _repo()
    repo.list = AsyncMock(return_value=object())

    await BrainSessionService(repo).list(project_key="brain-v42", status="stale")

    repo.list.assert_awaited_once_with(project_key="brain-v42", status="stale", limit=20, offset=0)
