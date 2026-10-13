"""Operator lifecycle commands must leave server-owned traces untouched."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_session_checkpoints,
    brain_session_connections,
    brain_sessions,
    decisions,
)
from brain_v42.models.brain_session import BrainSessionIdentityConflictError
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.services.brain_session_service import BrainSessionService
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slots import base_state

session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
SessionFactory = async_sessionmaker[AsyncSession]


def sessions(factory: SessionFactory) -> BrainSessionService:
    return BrainSessionService(PgBrainSessionRepo(factory))


@pytest_asyncio.fixture
async def agent_trace(session_factory: SessionFactory, slot_project: str) -> tuple[UUID, str]:
    key = f"auto:{uuid4().hex}"
    async with session_factory.begin() as session:
        trace_id = await session.scalar(
            brain_sessions.insert()
            .values(
                project_key=slot_project,
                client_key=key,
                started_focus_revision=0,
                nature="agent",
            )
            .returning(brain_sessions.c.id)
        )
    assert isinstance(trace_id, UUID)
    return trace_id, key


async def trace_state(factory: SessionFactory, trace_id: UUID) -> list[list[dict[str, Any]]]:
    """Snapshot the row and its ledgers so a refusal cannot hide partial writes."""
    result: list[list[dict[str, Any]]] = []
    async with factory() as session:
        for table in (
            brain_sessions,
            brain_session_artifacts,
            brain_session_checkpoints,
            brain_session_connections,
        ):
            identity = table.c.id if table is brain_sessions else table.c.session_id
            rows = (await session.execute(sa.select(table).where(identity == trace_id))).mappings()
            result.append([dict(row) for row in rows])
    return result


async def test_end_refuses_a_trace_without_writing_the_project_base(
    session_factory: SessionFactory, slot_project: str, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    before = await trace_state(session_factory, trace_id)
    focus_before = await base_state(session_factory, slot_project)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).end(
            trace_id,
            key,
            summary="attempted operator close",
            next_focus="a replacement of the entire project base focus",
            expected_focus_revision=focus_before[1],
            nothing_to_capture_reason="no operator artifacts",
        )
    assert refusal.value.code == "session_is_agent_trace"
    assert await base_state(session_factory, slot_project) == focus_before
    assert await trace_state(session_factory, trace_id) == before
    assert before[0][0]["status"] == "open"


async def test_abandon_refuses_a_trace_without_mutation(
    session_factory: SessionFactory, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    before = await trace_state(session_factory, trace_id)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).abandon(trace_id, key, "attempted operator abandonment")
    assert refusal.value.code == "session_is_agent_trace"
    assert await trace_state(session_factory, trace_id) == before


async def test_heartbeat_refuses_a_trace_without_mutation(
    session_factory: SessionFactory, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    before = await trace_state(session_factory, trace_id)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).heartbeat(trace_id, key)
    assert refusal.value.code == "session_is_agent_trace"
    assert await trace_state(session_factory, trace_id) == before


async def test_capture_refuses_a_trace_without_mutation(
    session_factory: SessionFactory, slot_project: str, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    async with session_factory.begin() as session:
        decision = await session.scalar(
            decisions.insert()
            .values(
                title="capture decision", description="d", reasoning="r", project_key=slot_project
            )
            .returning(decisions.c.id)
        )
    assert isinstance(decision, UUID)
    before = await trace_state(session_factory, trace_id)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).capture(trace_id, key, [decision])
    assert refusal.value.code == "session_is_agent_trace"
    assert await trace_state(session_factory, trace_id) == before


async def test_checkpoint_refuses_a_trace_without_mutation(
    session_factory: SessionFactory, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    before = await trace_state(session_factory, trace_id)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).checkpoint(
            trace_id, key, seq=1, progress="operator progress", next_step="operator next step"
        )
    assert refusal.value.code == "session_is_agent_trace"
    assert await trace_state(session_factory, trace_id) == before


async def test_resume_refuses_a_trace_without_mutation(
    session_factory: SessionFactory, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, key = agent_trace
    before = await trace_state(session_factory, trace_id)
    with pytest.raises(FocusSlotError) as refusal:
        await sessions(session_factory).resume(trace_id, key)
    assert refusal.value.code == "session_is_agent_trace"
    assert await trace_state(session_factory, trace_id) == before


@pytest.mark.parametrize(
    "operation", ["resume", "capture", "checkpoint", "heartbeat", "end", "abandon"]
)
async def test_a_wrong_trace_key_still_gets_the_identity_error_first(
    session_factory: SessionFactory,
    agent_trace: tuple[UUID, str],
    operation: str,
) -> None:
    trace_id, _key = agent_trace
    before = await trace_state(session_factory, trace_id)
    arguments: dict[str, dict[str, Any]] = {
        "resume": {},
        "capture": {"knowledge_ids": [uuid4()]},
        "checkpoint": {"seq": 1, "progress": "progress", "next_step": "next"},
        "heartbeat": {},
        "end": {"summary": "summary", "next_focus": "next focus", "expected_focus_revision": 0},
        "abandon": {"reason": "reason"},
    }
    with pytest.raises(BrainSessionIdentityConflictError):
        await getattr(sessions(session_factory), operation)(
            trace_id, "wrong-key", **arguments[operation]
        )
    assert await trace_state(session_factory, trace_id) == before


async def test_list_masks_trace_keys_and_preserves_operator_keys(
    session_factory: SessionFactory, slot_project: str, agent_trace: tuple[UUID, str]
) -> None:
    trace_id, trace_key = agent_trace
    svc = sessions(session_factory)
    operator = await svc.start(slot_project, f"operator-{uuid4().hex}")
    before = await trace_state(session_factory, trace_id)
    listed = await svc.list(project_key=slot_project, status="all")
    by_id = {item.id: item for item in listed.sessions}
    assert by_id[trace_id].client_key == "auto:<masked>"
    assert by_id[operator.session.id].client_key == operator.session.client_key
    assert trace_key not in listed.model_dump_json()
    assert await trace_state(session_factory, trace_id) == before
