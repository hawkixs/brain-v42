"""brain_session_relay against PostgreSQL: one transaction, S8 and S9, and the races."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slot
from brain_v42.db.tables import (
    brain_sessions,
    decisions,
    focus_slot_history,
    focus_slots,
    project_focus_history,
)
from brain_v42.models.brain_session import (
    BrainSessionFocusOutcome,
    BrainSessionInputError,
    BrainSessionTerminalConflictError,
)
from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import open_slot, sessions, started
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
        "expected_focus_revision": None,
        "allow_focus_shrink": False,
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


async def test_each_session_kind_is_refused_the_other_kinds_revision(session_factory, slot_project):
    loose, loose_key = await started(session_factory, slot_project)
    with pytest.raises(
        FocusSlotError, match="^relay_expects_focus_revision: .*expected_focus_revision"
    ):
        await relay(session_factory, loose, loose_key)  # a slot revision, for an unbound session
    with pytest.raises(FocusSlotError, match="^relay_expects_focus_revision: "):
        await relay(session_factory, loose, loose_key, expected_focus_revision=0)  # both
    anchored, key, _slot_id = await bound(session_factory, slot_project)
    with pytest.raises(
        FocusSlotError, match="^relay_expects_slot_revision: .*expected_slot_revision"
    ):
        await relay(
            session_factory, anchored, key, expected_slot_revision=None, expected_focus_revision=0
        )
    for untouched in (loose, anchored):
        assert (await row(session_factory, untouched))["status"] == "open"
        assert await successors(session_factory, untouched) == []


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


# ─── the base relay: an unbound session relayed onto the project base (ticket 64ebd73a) ─────


async def set_base(factory, project: str, text: str) -> int:
    """Put `text` on the project base through a plain unbound end; return the new revision."""
    session_id, key = await started(factory, project)
    revision = (await base_state(factory, project))[1]
    await sessions(factory).end(session_id, key, "seed the base", text, revision)
    return revision + 1


def base_relay(factory, session_id: UUID, key: str, revision: int, **overrides):
    overrides = {"expected_slot_revision": None, "expected_focus_revision": revision, **overrides}
    return relay(factory, session_id, key, **overrides)


async def base_history_sources(factory, project: str, revision: int) -> list[str]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(project_focus_history.c.source).where(
                        project_focus_history.c.project_key == project,
                        project_focus_history.c.focus_revision == revision,
                    )
                )
            ).scalars()
        )


async def test_a_base_relay_writes_the_base_and_starts_an_unbound_successor(
    session_factory, slot_project
):
    new_base = "durable base " * 4 + "carried"
    revision = await set_base(session_factory, slot_project, "durable base " * 4)
    session_id, key = await started(session_factory, slot_project)
    result = await base_relay(session_factory, session_id, key, revision, handover=new_base)
    old, new = await row(session_factory, session_id), await row(session_factory, result.session.id)
    assert (result.replayed, result.slot, result.focus_revision) == (False, None, revision + 1)
    state = await base_state(session_factory, slot_project)
    assert state[:2] == (new_base, revision + 1)
    assert (old["status"], old["focus_outcome"], old["next_focus"]) == (
        "ended",
        "applied",
        new_base,
    )
    assert (old["end_expected_focus_revision"], old["focus_revision_at_end"]) == (
        revision,
        revision + 1,
    )
    assert (new["status"], new["slot_id"], new["relayed_from_session_id"]) == (
        "open",
        None,
        None,
    )
    assert (new["started_focus"], new["started_focus_revision"], new["started_by_actor"]) == (
        state[0],
        revision + 1,
        "relay:operator",
    )
    assert new["started_at"] == old["ended_at"]
    assert await base_history_sources(session_factory, slot_project, revision + 1) == [
        "session_end"
    ]


async def test_a_base_relay_captures_the_named_knowledge(session_factory, slot_project):
    revision = await set_base(session_factory, slot_project, "base")
    session_id, key = await started(session_factory, slot_project)
    async with session_factory.begin() as session:
        decision = await session.scalar(
            decisions.insert()
            .values(title="base relay", description="d", reasoning="r", project_key=slot_project)
            .returning(decisions.c.id)
        )
    await base_relay(session_factory, session_id, key, revision, knowledge_ids=[decision])
    assert (await row(session_factory, session_id))["captured_knowledge_ids"] == [decision]


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "shrink",
        "client_key",
        "capture",
    ],
)
async def test_a_refused_base_relay_mutates_nothing_and_leaves_the_session_open(
    session_factory, slot_project, case
):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    overrides: dict[str, object] = {"handover": "y" * 100}
    code = {
        "stale": "focus_revision_conflict",
        "shrink": "base_focus_shrink",
        "client_key": "client_key_conflict",
        "capture": "invalid ids",
    }[case]
    if case == "stale":
        revision += 1
    elif case == "shrink":
        overrides["handover"] = "y" * 69
    elif case == "client_key":
        taken, _ = await started(session_factory, slot_project)
        overrides["new_client_key"] = (await row(session_factory, taken))["client_key"]
    else:
        overrides["knowledge_ids"] = [uuid4()]
    before = await base_state(session_factory, slot_project)
    sessions_before = await project_sessions(session_factory, slot_project)
    with pytest.raises((FocusSlotError, BrainSessionInputError), match=code):
        await base_relay(session_factory, session_id, key, revision, **overrides)
    assert await base_state(session_factory, slot_project) == before
    assert (await row(session_factory, session_id))["status"] == "open"
    # A base successor has no `relayed_from_session_id`: compare the project's sessions.
    assert await project_sessions(session_factory, slot_project) == sessions_before


async def test_the_shrink_refusal_gives_both_lengths_and_the_operator_may_override_it(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "x" * 100)
    session_id, key = await started(session_factory, slot_project)
    with pytest.raises(FocusSlotError, match="^base_focus_shrink: ") as refused:
        await base_relay(session_factory, session_id, key, revision, handover="y" * 69)
    assert "69" in str(refused.value) and "100" in str(refused.value)
    assert "REPLACES" in str(refused.value)
    # 70 is exactly 0.7 of 100: the guard refuses strictly below it.
    await base_relay(session_factory, session_id, key, revision, handover="y" * 70)
    other, other_key = await started(session_factory, slot_project)
    await base_relay(
        session_factory, other, other_key, revision + 1, handover="short", allow_focus_shrink=True
    )
    assert (await base_state(session_factory, slot_project))[:2] == ("short", revision + 2)


async def test_a_base_relay_replays_and_any_other_payload_is_terminal_conflict(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "base")
    session_id, key = await started(session_factory, slot_project)
    first = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-b")
    state = await base_state(session_factory, slot_project)
    sessions_after = await project_sessions(session_factory, slot_project)
    again = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-b")
    assert (again.replayed, again.session.id, again.slot) == (True, first.session.id, None)
    assert again.focus_revision == first.focus_revision == revision + 1
    assert await base_state(session_factory, slot_project) == state
    assert await project_sessions(session_factory, slot_project) == sessions_after
    for change in (
        {"expected_focus_revision": revision + 1},
        {"handover": "another"},
        {"expected_slot_revision": 1, "expected_focus_revision": None},
    ):
        with pytest.raises(FocusSlotError, match="^terminal_conflict: "):
            await base_relay(
                session_factory, session_id, key, revision, new_client_key="succ-b", **change
            )


async def test_a_base_relay_successor_has_no_relayed_from_and_is_found_by_its_key(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "base")
    session_id, key = await started(session_factory, slot_project)
    first = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-l")
    successor = await row(session_factory, first.session.id)
    # The 060 CHECK keeps `relayed_from_session_id` slot-only: the link is key + boundary.
    assert (successor["relayed_from_session_id"], successor["slot_id"]) == (None, None)
    assert await successors(session_factory, session_id) == []
    assert successor["started_at"] == (await row(session_factory, session_id))["ended_at"]
    again = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-l")
    assert (again.replayed, again.session.id) == (True, first.session.id)
    with pytest.raises(FocusSlotError, match="^terminal_conflict: "):
        await base_relay(session_factory, session_id, key, revision, new_client_key="other-key")


async def test_a_base_relay_still_replays_after_its_successor_bound_to_a_slot(
    session_factory, slot_project
):
    revision = await set_base(session_factory, slot_project, "base")
    session_id, key = await started(session_factory, slot_project)
    first = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-bd")
    slot_id = await open_slot(session_factory, slot_project)
    await sessions(session_factory).bind(first.session.id, "succ-bd", slot_id)
    # The form is read from the lineage column, not the slot: a lost response retried after
    # the successor bound must replay, not turn into terminal_conflict.
    again = await base_relay(session_factory, session_id, key, revision, new_client_key="succ-bd")
    assert (again.replayed, again.session.id, again.focus_revision) == (
        True,
        first.session.id,
        revision + 1,
    )


class _PausedAfterTheBaseLock(PgBrainSessionRepo):
    """A base relay that stops right after taking its lock, before it inserts its successor."""

    def __init__(self, factory) -> None:
        super().__init__(factory)
        self.locked = asyncio.Event()
        self.resume = asyncio.Event()

    async def _mark_ended(self, *args, **kwargs):
        self.locked.set()
        await self.resume.wait()
        return await super()._mark_ended(*args, **kwargs)


async def test_a_start_with_the_relays_new_key_cannot_deadlock_a_base_relay(
    session_factory, slot_project
):
    """The base lock must tolerate the FK KEY SHARE of a concurrent session insert.

    start inserts its row (placing the unique entry for the key, then needing KEY SHARE on
    the project row for the FK); the relay, holding the base, then inserts the same key and
    waits on that entry. With FOR UPDATE that is a cycle PostgreSQL breaks with a deadlock
    error; the relay must instead lose cleanly with `client_key_conflict`.
    """
    revision = await set_base(session_factory, slot_project, "base")
    session_id, key = await started(session_factory, slot_project)
    repo = _PausedAfterTheBaseLock(session_factory)
    relaying = asyncio.create_task(
        repo.relay(
            session_id,
            key,
            summary="s",
            handover="base",
            expected_focus_revision=revision,
            new_client_key="race-key",
            initiator="operator",
            knowledge_ids=[],
            nothing_to_capture_reason=None,
        )
    )
    await asyncio.wait_for(repo.locked.wait(), 10)
    starting = asyncio.create_task(sessions(session_factory).start(slot_project, "race-key"))
    await asyncio.sleep(1.5)  # past deadlock_timeout (1 s): start has inserted and is parked
    repo.resume.set()
    relayed, started_result = await asyncio.gather(relaying, starting, return_exceptions=True)
    assert getattr(relayed, "code", None) == "client_key_conflict", relayed
    assert not isinstance(started_result, BaseException), started_result
    assert (await row(session_factory, session_id))["status"] == "open"


async def test_the_base_lock_lets_a_session_insert_through(session_factory, slot_project):
    """The same lock mode, held the way `_relay_base` holds it, against the FK a session insert needs."""
    repo = PgBrainSessionRepo(session_factory)
    async with session_factory.begin() as holder:
        await repo._load_focus(holder, slot_project, for_no_key_update=True)
        async with session_factory.begin() as other:
            await other.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            await other.execute(
                brain_sessions.insert().values(
                    project_key=slot_project, client_key="fk-probe", started_focus_revision=0
                )
            )


async def test_a_slot_retry_naming_a_coincident_base_successors_key_is_terminal_conflict(
    session_factory, slot_project
):
    """The slot lookup and the base lookup must not both answer (one row each at one boundary)."""
    anchored, anchored_key, _slot_id = await bound(session_factory, slot_project)
    loose, loose_key = await started(session_factory, slot_project)
    revision = await set_base(session_factory, slot_project, "base")
    base = await base_relay(session_factory, loose, loose_key, revision, new_client_key="b-key")
    slot = await relay(session_factory, anchored, anchored_key, new_client_key="s-key")
    boundary = (await row(session_factory, loose))["ended_at"]
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update().where(brain_sessions.c.id == anchored).values(ended_at=boundary)
        )
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == slot.session.id)
            .values(started_at=boundary)
        )
    assert (await row(session_factory, base.session.id))["started_at"] == boundary
    with pytest.raises(FocusSlotError, match="^terminal_conflict: "):
        await relay(session_factory, anchored, anchored_key, new_client_key="b-key")


async def test_a_plain_ended_bound_session_never_replays_as_a_coincident_base_relay(
    session_factory, slot_project
):
    """The base fallback is for an UNBOUND predecessor only (round 2 review of #289)."""
    anchored, anchored_key, _slot_id = await bound(session_factory, slot_project)
    await sessions(session_factory).end(anchored, anchored_key, "same summary", "same body", 0)
    loose, loose_key = await started(session_factory, slot_project)
    revision = await set_base(session_factory, slot_project, "same body")
    base = await base_relay(
        session_factory,
        loose,
        loose_key,
        revision,
        summary="same summary",
        handover="same body",
        new_client_key="b-plain",
    )
    boundary = (await row(session_factory, loose))["ended_at"]
    # Make the bound session's terminal payload equal to the base relay's, at its boundary.
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == anchored)
            .values(
                ended_at=boundary,
                end_expected_focus_revision=revision,
                focus_revision_at_end=revision + 1,
            )
        )
    assert (await row(session_factory, base.session.id))["started_at"] == boundary
    with pytest.raises(FocusSlotError, match="^terminal_conflict: "):
        await base_relay(
            session_factory,
            anchored,
            anchored_key,
            revision,
            summary="same summary",
            handover="same body",
            new_client_key="b-plain",
        )
