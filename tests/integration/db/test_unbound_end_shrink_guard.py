"""An unbound `end` writes the base focus whole: it carries the relay's shrink guard (91faa1a8)."""

from __future__ import annotations

import pytest

from brain_v42.db.tables import project_contexts
from brain_v42.models.brain_session import BrainSessionFocusOutcome
from brain_v42.models.focus_slot import FocusSlotError
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions, started
from tests.integration.db.test_delivery_focus_slot_bound_end import bound
from tests.integration.db.test_delivery_focus_slot_relay import (
    base_history_sources,
    project_sessions,
    row,
    set_base,
)
from tests.integration.db.test_delivery_focus_slots import base_state

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def end(factory, session_id, key, revision, next_focus, **overrides):
    return sessions(factory).end(session_id, key, "summary", next_focus, revision, **overrides)


async def test_a_shrinking_end_is_refused_and_mutates_nothing(session_factory, slot_project):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    before = await base_state(session_factory, slot_project)
    sessions_before = await project_sessions(session_factory, slot_project)

    with pytest.raises(FocusSlotError, match="^base_focus_shrink: ") as refused:
        await end(session_factory, session_id, key, revision, "y" * 69)

    assert "69" in str(refused.value) and "100" in str(refused.value)
    assert "70" in str(refused.value) and "REPLACES" in str(refused.value)
    assert await base_state(session_factory, slot_project) == before
    assert (await row(session_factory, session_id))["status"] == "open"
    assert await project_sessions(session_factory, slot_project) == sessions_before


async def test_the_floor_is_inclusive_and_the_session_can_retry_after_a_refusal(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^base_focus_shrink: "):
        await end(session_factory, session_id, key, revision, "y" * 69)

    result = await end(session_factory, session_id, key, revision, "y" * 70)

    assert result.focus_outcome is BrainSessionFocusOutcome.APPLIED
    assert (await base_state(session_factory, slot_project))[:2] == ("y" * 70, revision + 1)


async def test_the_operator_may_override_the_guard_and_the_base_is_still_written(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)

    result = await end(session_factory, session_id, key, revision, "short", allow_focus_shrink=True)

    assert result.focus_outcome is BrainSessionFocusOutcome.APPLIED
    assert (await base_state(session_factory, slot_project))[:2] == ("short", revision + 1)
    assert await base_history_sources(session_factory, slot_project, revision + 1) == [
        "session_end"
    ]


async def test_an_empty_current_focus_has_nothing_to_guard(session_factory, slot_project):
    await set_base(session_factory, slot_project, "x" * 100)
    # An empty base cannot be written through a tool (a focus is at least one character), so
    # the fixture sets it the way a fresh project reads: the column back to ''.
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.update()
            .where(project_contexts.c.project_key == slot_project)
            .values(current_focus="")
        )
    session_id, key = await started(session_factory, slot_project)
    revision = (await base_state(session_factory, slot_project))[1]

    result = await end(session_factory, session_id, key, revision, "a")

    assert result.focus_outcome is BrainSessionFocusOutcome.APPLIED


async def test_a_replayed_end_is_not_guarded_again(session_factory, slot_project):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    await end(session_factory, session_id, key, revision, "short", allow_focus_shrink=True)
    state = await base_state(session_factory, slot_project)

    again = await end(session_factory, session_id, key, revision, "short")

    assert again.replayed is True
    assert await base_state(session_factory, slot_project) == state


async def test_a_stale_revision_wins_over_the_guard_and_the_end_closes_anyway(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    before = await base_state(session_factory, slot_project)

    # Stale AND shrinking: no base write is attempted, so there is nothing to refuse.
    result = await end(session_factory, session_id, key, revision - 1, "short")

    assert result.focus_outcome is BrainSessionFocusOutcome.CONFLICT
    assert await base_state(session_factory, slot_project) == before
    assert (await row(session_factory, session_id))["status"] == "ended"


async def test_a_bound_end_writes_its_slot_and_never_meets_the_base_guard(
    session_factory, slot_project
):
    await set_base(session_factory, slot_project, "x" * 100)
    session_id, key, _ = await bound(session_factory, slot_project)
    before = await base_state(session_factory, slot_project)

    result = await end(session_factory, session_id, key, 0, "short")

    assert result.focus_outcome is BrainSessionFocusOutcome.APPLIED
    assert await base_state(session_factory, slot_project) == before
