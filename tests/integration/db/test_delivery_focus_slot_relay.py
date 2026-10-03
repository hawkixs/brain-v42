"""brain_session_relay against PostgreSQL: one transaction, S8 and S9, and the races."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slot
from brain_v42.db.tables import brain_sessions, decisions, focus_slot_history, focus_slots
from brain_v42.models.brain_session import (
    BrainSessionFocusOutcome,
    BrainSessionInputError,
    BrainSessionTerminalConflictError,
)
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions, started
from tests.integration.db.test_delivery_focus_slot_bound_end import bound, history
from tests.integration.db.test_delivery_focus_slots import base_state

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def relay(factory, session_id: UUID, key: str, **overrides):
    """The repository transaction itself; Task 10 adds the service guards in front."""
    values: dict[str, object] = {
        "summary": "session summary",
        "handover": "handover body",
        "expected_slot_revision": 0,
        "new_client_key": f"next-{uuid4().hex[:8]}",
        "initiator": "operator",
        "knowledge_ids": [],
        "nothing_to_capture_reason": None,
    }
    values.update(overrides)
    return PgBrainSessionRepo(factory).relay(session_id, key, **values)


async def row(factory, session_id: UUID):
    async with factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def successors(factory, session_id: UUID) -> list[UUID]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(brain_sessions.c.id).where(
                        brain_sessions.c.relayed_from_session_id == session_id
                    )
                )
            ).scalars()
        )


async def open_on_slot(factory, slot_id: UUID) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(brain_sessions)
                .where(brain_sessions.c.slot_id == slot_id, brain_sessions.c.status == "open")
            )
        )


async def project_sessions(factory, project: str) -> list[tuple[UUID, str]]:
    async with factory() as session:
        rows = await session.execute(
            sa.select(brain_sessions.c.id, brain_sessions.c.status)
            .where(brain_sessions.c.project_key == project)
            .order_by(brain_sessions.c.id)
        )
        return [(r[0], r[1]) for r in rows]


async def test_a_relay_ends_writes_the_slot_and_starts_the_successor_in_one_go(
    session_factory, slot_project
):
    before = await base_state(session_factory, slot_project)
    session_id, key, slot_id = await bound(session_factory, slot_project)
    result = await relay(session_factory, session_id, key, new_client_key="successor-1")
    old = await row(session_factory, session_id)
    new = await row(session_factory, result.session.id)
    assert result.replayed is False and result.ended_session_id == session_id
    assert (old["status"], old["focus_outcome"], old["next_focus"], old["focus_at_end"]) == (
        "ended",
        "applied",
        "handover body",
        "handover body",
    )
    assert (old["end_expected_focus_revision"], old["focus_revision_at_end"]) == (0, 1)
    assert (new["status"], new["slot_id"], new["relayed_from_session_id"]) == (
        "open",
        slot_id,
        session_id,
    )
    assert (new["client_key"], new["started_by_actor"]) == ("successor-1", "relay:operator")
    assert new["started_focus_revision"] == before[1]  # the BASE revision
    assert new["started_at"] == old["ended_at"]
    assert (result.slot.revision, result.slot.body) == (1, "handover body")
    assert [(r[1], r[2]) for r in await history(session_factory, slot_id)] == [
        ("slot_open", None),
        ("session_relay", session_id),
    ]
    assert await base_state(session_factory, slot_project) == before  # S1


async def test_a_relay_captures_the_named_knowledge_into_the_old_session(
    session_factory, slot_project
):
    session_id, key, _slot_id = await bound(session_factory, slot_project)
    async with session_factory.begin() as session:
        decision = await session.scalar(
            decisions.insert()
            .values(
                title="relay decision", description="d", reasoning="r", project_key=slot_project
            )
            .returning(decisions.c.id)
        )
    await relay(session_factory, session_id, key, knowledge_ids=[decision])
    assert (await row(session_factory, session_id))["captured_knowledge_ids"] == [decision]


async def test_an_unbound_session_cannot_be_relayed_s14(session_factory, slot_project):
    session_id, key = await started(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^relay_requires_slot: "):
        await relay(session_factory, session_id, key)
    assert (await row(session_factory, session_id))["status"] == "open"


async def test_an_agent_trace_session_cannot_be_relayed(session_factory, slot_project):
    async with session_factory.begin() as session:
        trace = await session.scalar(
            brain_sessions.insert()
            .values(
                project_key=slot_project,
                client_key=f"trace-{uuid4().hex[:6]}",
                started_focus_revision=0,
                nature="agent",
            )
            .returning(brain_sessions.c.id)
        )
        key = await session.scalar(
            sa.select(brain_sessions.c.client_key).where(brain_sessions.c.id == trace)
        )
    with pytest.raises(FocusSlotError, match="^session_is_agent_trace: "):
        await relay(session_factory, trace, key)
    assert (await row(session_factory, trace))["status"] == "open"


async def test_a_capture_error_refuses_the_relay_and_mutates_nothing(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    slot_before = await history(session_factory, slot_id)
    with pytest.raises(BrainSessionInputError, match="invalid ids"):
        await relay(session_factory, session_id, key, knowledge_ids=[uuid4()])
    assert (await row(session_factory, session_id))["status"] == "open"
    assert await successors(session_factory, session_id) == []
    assert await history(session_factory, slot_id) == slot_before


@pytest.mark.parametrize("case", ["stale", "closed", "client_key"])
async def test_a_refused_relay_mutates_nothing_s8(session_factory, slot_project, case):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    overrides: dict[str, object] = {}
    if case == "stale":
        overrides["expected_slot_revision"] = 5
        code = "slot_revision_conflict"
    elif case == "closed":
        async with session_factory.begin() as session:
            await close_slot(session, slot_id=slot_id, reason=f"receipt:{uuid4()}", note=None)
        overrides["expected_slot_revision"] = 1
        code = "slot_closed"
    else:
        taken, _ = await started(session_factory, slot_project)
        overrides["new_client_key"] = (await row(session_factory, taken))["client_key"]
        code = "client_key_conflict"
    slot_before = await history(session_factory, slot_id)
    with pytest.raises(FocusSlotError, match=f"^{code}: "):
        await relay(session_factory, session_id, key, **overrides)
    assert (await row(session_factory, session_id))["status"] == "open"
    assert await successors(session_factory, session_id) == []
    assert await history(session_factory, slot_id) == slot_before


async def test_a_closed_slot_refusal_names_the_way_out(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    async with session_factory.begin() as session:
        await close_slot(session, slot_id=slot_id, reason=f"receipt:{uuid4()}", note=None)
    with pytest.raises(FocusSlotError) as refused:
        await relay(session_factory, session_id, key, expected_slot_revision=1)
    assert "expected_focus_revision=0" in str(refused.value)
    assert "abandon" in str(refused.value)


async def test_an_equal_replay_returns_the_same_successor_s9(session_factory, slot_project):
    session_id, key, _slot_id = await bound(session_factory, slot_project)
    first = await relay(session_factory, session_id, key, new_client_key="successor-r")
    again = await relay(session_factory, session_id, key, new_client_key="successor-r")
    assert (again.replayed, again.session.id) == (True, first.session.id)
    assert await successors(session_factory, session_id) == [first.session.id]


async def test_an_identical_replay_writes_nothing(session_factory, slot_project):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    first = await relay(session_factory, session_id, key, new_client_key="successor-w")
    history_before = await history(session_factory, slot_id)
    sessions_before = await project_sessions(session_factory, slot_project)
    again = await relay(session_factory, session_id, key, new_client_key="successor-w")
    assert (again.replayed, again.session.id) == (True, first.session.id)
    assert await history(session_factory, slot_id) == history_before
    assert await project_sessions(session_factory, slot_project) == sessions_before
    assert again.slot.revision == first.slot.revision


@pytest.mark.parametrize(
    "change",
    [
        {"new_client_key": "another-key"},
        {"summary": "another summary"},
        {"handover": "another handover"},
        {"expected_slot_revision": 1},
        {"knowledge_ids": [uuid4()]},  # an UNCAPTURED id: the replay must not widen the ledger
    ],
)
async def test_any_other_payload_on_a_relayed_session_is_terminal_conflict(
    session_factory, slot_project, change
):
    session_id, key, _slot_id = await bound(session_factory, slot_project)
    first = await relay(session_factory, session_id, key, new_client_key="successor-t")
    with pytest.raises(FocusSlotError, match=f"^terminal_conflict: .*{first.session.id}"):
        await relay(session_factory, session_id, key, **{"new_client_key": "successor-t", **change})


async def test_a_session_ended_by_a_plain_end_is_terminal_conflict(session_factory, slot_project):
    session_id, key, _slot_id = await bound(session_factory, slot_project)
    await sessions(session_factory).end(session_id, key, "session summary", "handover body", 0)
    with pytest.raises(FocusSlotError, match="^terminal_conflict: "):
        await relay(session_factory, session_id, key)


async def test_the_same_relay_twice_concurrently_yields_one_successor(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    first, second = await asyncio.gather(
        relay(session_factory, session_id, key, new_client_key="successor-c"),
        relay(session_factory, session_id, key, new_client_key="successor-c"),
    )
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert first.session.id == second.session.id
    assert await open_on_slot(session_factory, slot_id) == 1


async def test_two_relays_with_different_keys_one_successor_one_terminal_conflict(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    outcomes = await asyncio.gather(
        relay(session_factory, session_id, key, new_client_key="successor-a"),
        relay(session_factory, session_id, key, new_client_key="successor-b"),
        return_exceptions=True,
    )
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1 and getattr(errors[0], "code", None) == "terminal_conflict"
    assert len(await successors(session_factory, session_id)) == 1
    assert await open_on_slot(session_factory, slot_id) == 1


async def test_a_relay_racing_a_bind_never_leaves_two_sessions_open_on_the_slot(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    third, third_key = await started(session_factory, slot_project)
    outcomes = await asyncio.gather(
        relay(session_factory, session_id, key),
        sessions(session_factory).bind(third, third_key, slot_id),
        return_exceptions=True,
    )
    assert getattr(outcomes[1], "code", None) == "slot_busy"
    assert not isinstance(outcomes[0], BaseException)
    assert await open_on_slot(session_factory, slot_id) == 1


async def test_an_end_racing_a_relay_of_the_same_session_lets_exactly_one_win(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    outcomes = await asyncio.gather(
        relay(session_factory, session_id, key),
        sessions(session_factory).end(session_id, key, "summary", "end body", 0),
        return_exceptions=True,
    )
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], (FocusSlotError, BrainSessionTerminalConflictError))
    async with session_factory() as session:
        rows = await session.execute(
            sa.select(focus_slot_history.c.source).where(focus_slot_history.c.slot_id == slot_id)
        )
        sources = sorted(rows.scalars())
    assert sources in (["session_end", "slot_open"], ["session_relay", "slot_open"])
    assert (await row(session_factory, session_id))["focus_outcome"] == (
        BrainSessionFocusOutcome.APPLIED.value
    )
    async with session_factory() as session:
        revision = await session.scalar(
            sa.select(focus_slots.c.revision).where(focus_slots.c.id == slot_id)
        )
    assert revision == 1


async def through_service(factory, session_id: UUID, key: str, **overrides):
    """The service guards in front of the same transaction."""
    values: dict[str, object] = {
        "summary": "session summary",
        "handover": "handover body",
        "expected_slot_revision": 0,
        "new_client_key": f"next-{uuid4().hex[:8]}",
        "initiator": "operator",
    }
    values.update(overrides)
    return await sessions(factory).relay(session_id, key, **values)


@pytest.mark.parametrize("length", [4000, 4001])
async def test_the_handover_bound_counts_non_ascii_characters_and_refuses_before_any_write(
    session_factory, slot_project, length
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    history_before = await history(session_factory, slot_id)
    handover = "é" * length  # 8,000 bytes at the bound: a byte count would refuse it
    if length > 4000:
        with pytest.raises(FocusSlotError, match="^slot_body_too_long: "):
            await through_service(session_factory, session_id, key, handover=handover)
        assert (await row(session_factory, session_id))["status"] == "open"
        assert await successors(session_factory, session_id) == []
        assert await history(session_factory, slot_id) == history_before
    else:
        result = await through_service(session_factory, session_id, key, handover=handover)
        assert result.slot.body == handover and result.replayed is False


async def test_the_service_refuses_guard_mod_and_a_reused_key_before_any_write(
    session_factory, slot_project
):
    session_id, key, slot_id = await bound(session_factory, slot_project)
    history_before = await history(session_factory, slot_id)
    with pytest.raises(FocusSlotError, match="^relay_guard_mod_disabled: "):
        await through_service(session_factory, session_id, key, initiator="guard_mod")
    with pytest.raises(FocusSlotError, match="^relay_same_client_key: "):
        await through_service(session_factory, session_id, key, new_client_key=key)
    assert (await row(session_factory, session_id))["status"] == "open"
    assert await history(session_factory, slot_id) == history_before
