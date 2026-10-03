from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import BrainSessionInputError
from brain_v42.services.brain_session_service import BrainSessionService


async def test_bind_normalises_the_identity_and_forwards() -> None:
    repo = MagicMock()
    repo.bind = AsyncMock(return_value="bound")
    session_id, slot_id = uuid4(), uuid4()
    assert await BrainSessionService(repo).bind(session_id, " key ", slot_id) == "bound"
    repo.bind.assert_awaited_once_with(session_id, "key", slot_id)


async def test_bind_refuses_a_blank_identity_before_the_repository() -> None:
    repo = MagicMock()
    repo.bind = AsyncMock()
    with pytest.raises(BrainSessionInputError, match="expected_client_key"):
        await BrainSessionService(repo).bind(uuid4(), "  ", uuid4())
    repo.bind.assert_not_awaited()
