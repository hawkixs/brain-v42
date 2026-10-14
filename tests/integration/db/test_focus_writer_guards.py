"""Focus replacements cannot bypass the slot guard or the base CAS."""

from typing import Any
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tools.project_context_tools import register_project_context_tools
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.project_context_service import ProjectContextService
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


async def test_unelevated_operator_relay_cannot_override_slot_shrink(
    session_factory: Any, slot_project: str
) -> None:
    session_id, key, slot_id = await long_bound(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^focus_shrink_override_refused: "):
        await sessions(session_factory).relay(
            session_id,
            key,
            summary="summary",
            handover="short",
            expected_slot_revision=1,
            new_client_key=f"successor-{uuid4().hex}",
            initiator="operator",
            allow_focus_shrink=True,
        )
    assert (await row(session_factory, session_id))["status"] == "open"
    assert (await slot_row(session_factory, slot_id))["revision"] == 1


async def test_elevated_operator_relay_can_override_slot_shrink(
    session_factory: Any, slot_project: str, elevated_caller: Any
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


async def test_existing_context_refuses_focus_overwrite(
    session_factory: Any, slot_project: str
) -> None:
    repo = PgProjectContextRepo(session_factory)
    before = await repo.get_by_key(slot_project)
    with pytest.raises(FocusSlotError, match="^focus_write_requires_update_project_focus: "):
        await ProjectContextService(repo).get_or_create(
            ProjectContextCreate(
                project_key=slot_project,
                name="changed",
                description="changed",
                current_focus="different focus",
            )
        )
    after = await repo.get_by_key(slot_project)
    assert (after.current_focus, after.focus_revision, after.name) == (
        before.current_focus,
        before.focus_revision,
        before.name,
    )


async def test_new_context_can_set_focus(session_factory: Any) -> None:
    context = await ProjectContextService(PgProjectContextRepo(session_factory)).get_or_create(
        ProjectContextCreate(
            project_key=f"focus-{uuid4().hex}",
            name="new",
            description="new",
            current_focus="initial focus",
        )
    )
    assert context.current_focus == "initial focus" and context.focus_revision == 0


async def test_context_metadata_update_keeps_focus(session_factory: Any, slot_project: str) -> None:
    repo = PgProjectContextRepo(session_factory)
    before = await repo.get_by_key(slot_project)
    updated = await repo.get_or_create(
        ProjectContextCreate(project_key=slot_project, name="renamed", description="metadata")
    )
    assert updated.name == "renamed"
    assert (updated.current_focus, updated.focus_revision, updated.focus_updated_at) == (
        before.current_focus,
        before.focus_revision,
        before.focus_updated_at,
    )


async def test_generic_context_update_cannot_write_focus(
    session_factory: Any, slot_project: str
) -> None:
    repo = PgProjectContextRepo(session_factory)
    before = await repo.get_by_key(slot_project)
    updated = await repo.update(
        before.id, ProjectContextUpdate(name="renamed", current_focus="unguarded replacement")
    )
    assert updated.name == "renamed"
    assert (updated.current_focus, updated.focus_revision, updated.focus_updated_at) == (
        before.current_focus,
        before.focus_revision,
        before.focus_updated_at,
    )


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


class _ToolRegistry:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


async def test_the_real_tool_refuses_a_different_focus_on_an_existing_project(
    session_factory: Any, slot_project: str
) -> None:
    """The refusal must survive the tool's own model building, not only the service's."""
    repo = PgProjectContextRepo(session_factory)
    before = await repo.get_by_key(slot_project)
    registry = _ToolRegistry()
    register_project_context_tools(registry, ProjectContextService(repo))

    with pytest.raises(ToolError, match="^focus_write_requires_update_project_focus: "):
        await registry.registered["brain_set_project_context"](
            project_key=slot_project,
            name="changed",
            description="changed",
            current_focus=f"a genuinely different focus {uuid4().hex}",
        )

    after = await repo.get_by_key(slot_project)
    assert (after.current_focus, after.focus_revision, after.name) == (
        before.current_focus,
        before.focus_revision,
        before.name,
    )
