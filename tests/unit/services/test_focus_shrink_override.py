"""A destructive replacement needs a verified elevation before any mutation."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.provenance import (
    set_current_principal,
    set_current_transport,
)
from brain_v42.services.brain_session_service import BrainSessionService


@pytest.mark.parametrize("writer", ["end"])
@pytest.mark.parametrize("identity", ["absent", "not_elevated", "checker_failure", "no_connection"])
async def test_override_is_refused_before_any_repository_call(
    writer: str, identity: str, caller: AsyncMock
) -> None:
    if identity != "absent":
        set_current_principal("workstation-claude")
    if identity == "checker_failure":
        caller.side_effect = RuntimeError("private repository failure")
    if identity == "no_connection":
        set_current_transport(None)
    repo = MagicMock()
    repo.end = AsyncMock()
    repo.absorb_derived_capture_outcome = AsyncMock()
    factory = MagicMock()
    with pytest.raises(FocusSlotError, match="^focus_shrink_override_refused: ") as refused:
        await BrainSessionService(repo).end(
            uuid4(), "operator", "summary", "short", 0, allow_focus_shrink=True
        )
    assert "/brain-admin" in str(refused.value) and "operator relay" in str(refused.value)
    assert "private repository failure" not in str(refused.value)
    repo.end.assert_not_awaited()
    repo.absorb_derived_capture_outcome.assert_not_awaited()
    factory.assert_not_called()
    if identity in {"absent", "no_connection"}:
        caller.assert_not_awaited()


async def test_elevated_end_passes_the_override(caller: AsyncMock) -> None:
    set_current_principal("workstation-claude")
    caller.return_value = True
    repo = MagicMock()
    repo.end = AsyncMock()
    repo.absorb_derived_capture_outcome = AsyncMock(return_value=None)
    await BrainSessionService(repo).end(
        uuid4(), "operator", "summary", "short", 0, allow_focus_shrink=True
    )
    assert repo.end.await_args.kwargs["allow_focus_shrink"] is True
    connection, principal, now = caller.await_args.args
    assert (connection, principal) == ("a" * 32, "workstation-claude")
    assert now.tzinfo is UTC and now <= datetime.now(UTC)
