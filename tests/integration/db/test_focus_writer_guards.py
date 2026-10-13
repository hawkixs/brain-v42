"""Slot replacements cannot bypass the 70% shrink guard or its operator override."""

from typing import Any
from uuid import uuid4

import pytest

from brain_v42.models.focus_slot import FocusSlotError
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions
from tests.integration.db.test_delivery_focus_slot_bound_end import bound, history, slot_row
from tests.integration.db.test_delivery_focus_slot_relay import relay, row

session_factory = _slots.session_factory
slot_project = _slots.slot_project
pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def long_bound(factory: Any, project: str) -> tuple[Any, str, Any]:
    session_id, key, slot_id = await bound(factory, project)
    await sessions(factory).end(session_id, key, "seed", "x" * 2643, 0)
    successor = await sessions(factory).start(project, f"holder-{uuid4().hex}")
    await sessions(factory).bind(successor.session.id, successor.session.client_key, slot_id)
    return successor.session.id, successor.session.client_key, slot_id


async def test_bound_end_refuses_a_destructive_replacement(
    session_factory: Any, slot_project: str
) -> None:
    session_id, key, slot_id = await long_bound(session_factory, slot_project)
    before = await slot_row(session_factory, slot_id)
    trail = await history(session_factory, slot_id)
    with pytest.raises(FocusSlotError, match="^slot_focus_shrink: ") as refused:
        await sessions(session_factory).end(session_id, key, "summary", "y" * 814, 1)
    assert "1829 characters" in str(refused.value) and "30.8%" in str(refused.value)
    assert "This write REPLACES the whole slot body" in str(refused.value)
    assert str(slot_id) in str(refused.value)
    assert await slot_row(session_factory, slot_id) == before
    assert await history(session_factory, slot_id) == trail
    assert (await row(session_factory, session_id))["status"] == "open"


@pytest.mark.parametrize("initiator", ["operator", "guard_mod"])
async def test_bound_relay_refuses_shrink_without_override(
    session_factory: Any, slot_project: str, initiator: str
) -> None:
    session_id, key, slot_id = await long_bound(session_factory, slot_project)
    before = await slot_row(session_factory, slot_id)
    trail = await history(session_factory, slot_id)
    with pytest.raises(FocusSlotError, match="^slot_focus_shrink: "):
        await relay(
            session_factory,
            session_id,
            key,
            handover="y" * 814,
            expected_slot_revision=1,
            initiator=initiator,
        )
    assert await slot_row(session_factory, slot_id) == before
    assert await history(session_factory, slot_id) == trail
    assert (await row(session_factory, session_id))["status"] == "open"


async def test_operator_relay_can_override_slot_shrink(
    session_factory: Any, slot_project: str
) -> None:
    session_id, key, slot_id = await long_bound(session_factory, slot_project)
    result = await sessions(session_factory).relay(
        session_id,
        key,
        summary="summary",
        handover="short",
        expected_slot_revision=1,
        new_client_key=f"successor-{uuid4().hex}",
        initiator="operator",
        allow_focus_shrink=True,
    )
    assert result.slot.body == "short" and result.slot.revision == 2
    assert (await row(session_factory, session_id))["status"] == "ended"
    assert (await slot_row(session_factory, slot_id))["body"] == "short"


async def test_elevated_end_can_override_slot_shrink(
    session_factory: Any, slot_project: str, elevated_caller: Any
) -> None:
    session_id, key, slot_id = await long_bound(session_factory, slot_project)
    result = await sessions(session_factory).end(
        session_id, key, "summary", "short", 1, allow_focus_shrink=True
    )
    assert (result.current_focus, result.current_focus_revision) == ("short", 2)
    assert (await slot_row(session_factory, slot_id))["body"] == "short"
    assert (await row(session_factory, session_id))["status"] == "ended"
