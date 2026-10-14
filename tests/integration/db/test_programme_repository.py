"""PgProgrammeRepo against PostgreSQL: authority, caps, transitions, publication.

Contract docs/contracts/programmes_v1.md §5 and §6. The caller is the project the
credential layer resolved; the repository receives it from the service and never
from a tool argument. Runs on the module-private head database: the live-programme
cap is global, so the shared head would make these tests depend on each other.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import programmes, project_contexts
from brain_v42.models.programme import ProgrammeError
from brain_v42.repositories.pg_programme import PgProgrammeRepo

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def factory(private_head_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker]:
    session_factory = async_sessionmaker(
        private_head_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield session_factory
    async with session_factory.begin() as session:
        await session.execute(sa.delete(programmes))


async def _project(factory: async_sessionmaker) -> str:
    key = f"prog-{uuid4().hex[:10]}"
    async with factory.begin() as session:
        await session.execute(
            pg_insert(project_contexts)
            .values(project_key=key, name=key, description="programme fixture")
            .on_conflict_do_nothing(index_elements=["project_key"])
        )
    return key


@pytest_asyncio.fixture
async def coordinator(factory: async_sessionmaker) -> str:
    return await _project(factory)


@pytest_asyncio.fixture
async def owner(factory: async_sessionmaker) -> str:
    return await _project(factory)


@pytest_asyncio.fixture
async def stranger(factory: async_sessionmaker) -> str:
    return await _project(factory)


async def _refusal(code: str, coroutine) -> ProgrammeError:
    with pytest.raises(ProgrammeError) as refused:
        await coroutine
    assert refused.value.code == code, refused.value
    return refused.value


async def test_the_creator_becomes_the_coordinator_and_the_server_signs(
    factory, coordinator: str
) -> None:
    repo = PgProgrammeRepo(factory)
    published = await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    assert published["coordinator"] == coordinator
    assert published["state"] == "active"
    assert published["last_change"] is None
    assert published["gates"] == []
    assert set(published) == {"key", "title", "coordinator", "state", "last_change", "gates"}


async def test_only_the_coordinator_changes_a_programme(
    factory, coordinator: str, stranger: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await _refusal(
        "not_coordinator",
        repo.upsert_programme(caller=stranger, key="move", title="Hijacked"),
    )
    updated = await repo.upsert_programme(
        caller=coordinator, key="move", title="Move v2", state="paused"
    )
    assert (updated["title"], updated["state"]) == ("Move v2", "paused")


async def test_a_gate_is_created_by_the_coordinator_and_published_in_full(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G6", title="t", owner=owner)
    published = await repo.upsert_gate(
        caller=coordinator,
        programme="move",
        gate="G7",
        title="red-backup on red-base",
        owner=owner,
        depends_on=["G6"],
        weight=4,
    )
    gate = published["gates"][1]
    assert gate == {
        "key": "G7",
        "title": "red-backup on red-base",
        "owner": owner,
        "state": "todo",
        "depends_on": ["G6"],
        "weight": 4,
        "waiting_on_operator": None,
        "progress": None,
        "note": None,
        "updated_at": gate["updated_at"],
        "updated_by": coordinator,
    }
    assert datetime.fromisoformat(gate["updated_at"]).utcoffset() == timedelta(0)
    assert published["last_change"] is None, "creating a gate is not a transition"


async def test_structure_is_the_coordinators_alone(factory, coordinator: str, owner: str) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await _refusal(
        "not_coordinator",
        repo.upsert_gate(caller=owner, programme="move", gate="G1", title="t", owner=owner),
    )
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G1", title="t", owner=owner)
    await _refusal(
        "not_coordinator",
        repo.upsert_gate(caller=owner, programme="move", gate="G1", weight=9),
    )


async def test_the_owner_reports_and_the_transition_is_recorded(
    factory, coordinator: str, owner: str, stranger: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G7", title="t", owner=owner)
    await _refusal(
        "not_gate_owner",
        repo.report_gate(caller=stranger, programme="move", gate="G7", state="done"),
    )
    published = await repo.report_gate(
        caller=owner,
        programme="move",
        gate="G7",
        state="in_progress",
        progress={"done": 6, "total": 15, "unit": "tasks"},
        note="first deploy",
    )
    change = published["last_change"]
    gate = published["gates"][0]
    assert (change["gate"], change["from"], change["to"], change["by"]) == (
        "G7",
        "todo",
        "in_progress",
        owner,
    )
    assert change["at"] == gate["updated_at"]
    assert gate["progress"] == {"done": 6, "total": 15, "unit": "tasks"}
    assert (gate["note"], gate["updated_by"]) == ("first deploy", owner)


async def test_reporting_the_same_state_refreshes_the_gate_but_not_last_change(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G7", title="t", owner=owner)
    first = await repo.report_gate(caller=owner, programme="move", gate="G7", state="in_progress")
    second = await repo.report_gate(
        caller=coordinator, programme="move", gate="G7", state="in_progress", note="still on it"
    )
    assert second["last_change"] == first["last_change"]
    assert second["gates"][0]["updated_at"] > first["gates"][0]["updated_at"]
    assert second["gates"][0]["updated_by"] == coordinator


async def test_progress_and_note_can_be_cleared(factory, coordinator: str, owner: str) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G7", title="t", owner=owner)
    await repo.report_gate(
        caller=owner,
        programme="move",
        gate="G7",
        progress={"done": 1, "total": 2, "unit": "t"},
        note="n",
    )
    cleared = await repo.report_gate(
        caller=owner, programme="move", gate="G7", clear_progress=True, clear_note=True
    )
    assert (cleared["gates"][0]["progress"], cleared["gates"][0]["note"]) == (None, None)


async def test_a_request_keeps_its_since_until_the_ask_changes_and_leaves_when_lifted(
    factory, coordinator: str, owner: str, stranger: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G7", title="t", owner=owner)
    await _refusal(
        "not_gate_owner", repo.request(caller=stranger, programme="move", gate="G7", ask="go")
    )
    raised = await repo.request(caller=owner, programme="move", gate="G7", ask="validate spec")
    since = raised["gates"][0]["waiting_on_operator"]["since"]
    again = await repo.request(caller=owner, programme="move", gate="G7", ask="validate spec")
    assert again["gates"][0]["waiting_on_operator"] == {"ask": "validate spec", "since": since}
    changed = await repo.request(caller=owner, programme="move", gate="G7", ask="other ask")
    assert changed["gates"][0]["waiting_on_operator"]["since"] > since
    lifted = await repo.request(caller=coordinator, programme="move", gate="G7", ask=None)
    assert lifted["gates"][0]["waiting_on_operator"] is None


async def test_dependencies_must_exist_in_the_programme_and_stay_acyclic(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await repo.upsert_gate(caller=coordinator, programme="move", gate="G1", title="t", owner=owner)
    await _refusal(
        "unknown_dependency",
        repo.upsert_gate(
            caller=coordinator,
            programme="move",
            gate="G2",
            title="t",
            owner=owner,
            depends_on=["G9"],
        ),
    )
    await repo.upsert_gate(
        caller=coordinator, programme="move", gate="G2", title="t", owner=owner, depends_on=["G1"]
    )
    await _refusal(
        "dependency_cycle",
        repo.upsert_gate(caller=coordinator, programme="move", gate="G1", depends_on=["G2"]),
    )
    await _refusal(
        "dependency_cycle",
        repo.upsert_gate(caller=coordinator, programme="move", gate="G1", depends_on=["G1"]),
    )


async def test_unknown_programme_gate_and_owner_are_named(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await _refusal(
        "programme_not_found", repo.report_gate(caller=owner, programme="nope", gate="G1")
    )
    await repo.upsert_programme(caller=coordinator, key="move", title="Move")
    await _refusal("gate_not_found", repo.report_gate(caller=owner, programme="move", gate="G1"))
    await _refusal(
        "gate_incomplete",
        repo.upsert_gate(caller=coordinator, programme="move", gate="G1", title="t"),
    )
    await _refusal(
        "owner_unknown",
        repo.upsert_gate(
            caller=coordinator, programme="move", gate="G1", title="t", owner="no-such-project"
        ),
    )


async def test_the_live_and_gate_caps_refuse_instead_of_growing(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory, max_live=2, max_gates=2)
    await repo.upsert_programme(caller=coordinator, key="a", title="A")
    await repo.upsert_programme(caller=coordinator, key="b", title="B", state="paused")
    await _refusal(
        "programme_limit_reached", repo.upsert_programme(caller=coordinator, key="c", title="C")
    )
    await repo.upsert_programme(caller=coordinator, key="c", title="C", state="done")
    await _refusal(
        "programme_limit_reached",
        repo.upsert_programme(caller=coordinator, key="c", title="C", state="active"),
    )
    for gate in ("G1", "G2"):
        await repo.upsert_gate(caller=coordinator, programme="a", gate=gate, title="t", owner=owner)
    await _refusal(
        "gate_limit_reached",
        repo.upsert_gate(caller=coordinator, programme="a", gate="G3", title="t", owner=owner),
    )


async def test_a_programme_past_its_byte_cap_is_refused_and_nothing_is_written(
    factory, coordinator: str, owner: str
) -> None:
    """The cap is measured on the published programme (contract §5: 16 KiB by default).

    The cap here is the measured size plus 100 bytes: one more gate (about 300 bytes)
    or a 200-character note crosses it, and the refused write leaves no trace.
    """
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="big", title="Big")
    for index in range(4):
        published = await repo.upsert_gate(
            caller=coordinator, programme="big", gate=f"G{index}", title="t" * 120, owner=owner
        )
    capped = PgProgrammeRepo(factory, max_bytes=len(json.dumps(published).encode()) + 100)
    before = await capped.list_programmes(include_done=True)
    await _refusal(
        "programme_too_large",
        capped.upsert_gate(
            caller=coordinator, programme="big", gate="G9", title="t" * 120, owner=owner
        ),
    )
    await _refusal(
        "programme_too_large",
        capped.report_gate(caller=owner, programme="big", gate="G0", note="n" * 200),
    )
    assert await capped.list_programmes(include_done=True) == before


async def test_the_published_block_keeps_live_ones_and_four_recent_done_ones(
    factory, coordinator: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="live", title="Live")
    await repo.upsert_programme(caller=coordinator, key="paused", title="P", state="paused")
    for index in range(6):
        await repo.upsert_programme(caller=coordinator, key=f"done{index}", title="D", state="done")
    async with factory.begin() as session:
        await session.execute(
            sa.update(programmes)
            .where(programmes.c.key == "done0")
            .values(updated_at=datetime.now(UTC) - timedelta(days=8))
        )
    block = await repo.published_block()
    assert block["version"] == "programmes/v1"
    assert "generated_at" not in block, "the sidecar cache stamps generated_at"
    keys = [programme["key"] for programme in block["programmes"]]
    assert keys[:2] == ["live", "paused"]
    assert sorted(keys[2:]) == ["done2", "done3", "done4", "done5"]
    assert len(json.dumps(block).encode()) < 16 * 1024


async def test_the_published_block_is_empty_but_present_with_no_programme(factory) -> None:
    assert await PgProgrammeRepo(factory).published_block() == {
        "version": "programmes/v1",
        "programmes": [],
    }


async def test_positions_are_bounded_and_the_next_one_never_overflows(
    factory, coordinator: str
) -> None:
    """Security pass 2026-10-13: max(position)+1 on a caller-chosen 2^31-1 overflowed
    INTEGER and blocked every later creation. Positions stop at 9999; the next free
    one is clamped there, ties ordered by creation."""
    repo = PgProgrammeRepo(factory)
    # The repository is the last line: even a row already at the INTEGER maximum
    # (written before the service bound, or by hand) must not break the next creation.
    await repo.upsert_programme(caller=coordinator, key="last", title="L", position=2_147_483_647)
    after = await repo.upsert_programme(caller=coordinator, key="after", title="A")
    assert after["key"] == "after"
    listed = [p["key"] for p in await repo.list_programmes()]
    assert listed == ["after", "last"]


async def test_one_coordinator_cannot_take_every_live_slot(
    factory, coordinator: str, stranger: str
) -> None:
    repo = PgProgrammeRepo(factory, max_live=3, max_live_per_coordinator=2)
    await repo.upsert_programme(caller=coordinator, key="a", title="A")
    await repo.upsert_programme(caller=coordinator, key="b", title="B")
    await _refusal(
        "coordinator_limit_reached",
        repo.upsert_programme(caller=coordinator, key="c", title="C"),
    )
    await repo.upsert_programme(caller=stranger, key="d", title="D")


async def test_one_coordinator_cannot_grow_the_table_without_bound(
    factory, coordinator: str
) -> None:
    repo = PgProgrammeRepo(factory, max_programmes_per_coordinator=2)
    await repo.upsert_programme(caller=coordinator, key="a", title="A", state="done")
    await repo.upsert_programme(caller=coordinator, key="b", title="B", state="done")
    await _refusal(
        "coordinator_limit_reached",
        repo.upsert_programme(caller=coordinator, key="c", title="C", state="done"),
    )


# -- Review round 1 of #377 ---------------------------------------------------------


async def test_publication_order_is_brains_across_states(factory, coordinator: str) -> None:
    """Contract §5: position, then creation, then key, over the whole selection."""
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="active", title="A", position=10)
    await repo.upsert_programme(
        caller=coordinator, key="done-first", title="D", position=0, state="done"
    )
    block = await repo.published_block()
    assert [p["key"] for p in block["programmes"]] == ["done-first", "active"]


async def test_a_done_programme_leaves_the_block_after_seven_days_but_stays_readable(
    factory, coordinator: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="recent", title="R", state="done")
    await repo.upsert_programme(caller=coordinator, key="expired", title="E", state="done")
    async with factory.begin() as session:
        await session.execute(
            sa.update(programmes)
            .where(programmes.c.key == "expired")
            .values(updated_at=datetime.now(UTC) - timedelta(days=7, minutes=1))
        )
    block = await repo.published_block()
    assert [p["key"] for p in block["programmes"]] == ["recent"]
    listed = await repo.list_programmes(key="expired")
    assert [p["key"] for p in listed] == ["expired"]
    assert "expired" in [p["key"] for p in await repo.list_programmes(include_done=True)]


async def test_the_contract_defaults_are_the_live_caps(factory) -> None:
    """Contract §5 with the shipped defaults: 8 live in all, 4 per coordinator."""
    repo = PgProgrammeRepo(factory)
    first = await _project(factory)
    second = await _project(factory)
    third = await _project(factory)
    for index in range(4):
        await repo.upsert_programme(caller=first, key=f"a{index}", title="A")
    await _refusal(
        "coordinator_limit_reached", repo.upsert_programme(caller=first, key="a4", title="A")
    )
    for index in range(4):
        await repo.upsert_programme(caller=second, key=f"b{index}", title="B")
    await _refusal(
        "programme_limit_reached", repo.upsert_programme(caller=third, key="c0", title="C")
    )


async def test_the_contract_default_gate_cap_is_32(factory, coordinator: str, owner: str) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="many", title="M")
    for index in range(32):
        await repo.upsert_gate(
            caller=coordinator, programme="many", gate=f"G{index}", title="t", owner=owner
        )
    await _refusal(
        "gate_limit_reached",
        repo.upsert_gate(caller=coordinator, programme="many", gate="G32", title="t", owner=owner),
    )


async def test_the_contract_default_byte_cap_is_16_kib(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="heavy", title="H")
    title, note = "t" * 120, "n" * 200
    refused = None
    for index in range(32):
        try:
            await repo.upsert_gate(
                caller=coordinator, programme="heavy", gate=f"G{index}", title=title, owner=owner
            )
            await repo.report_gate(caller=owner, programme="heavy", gate=f"G{index}", note=note)
        except ProgrammeError as error:
            refused = error
            break
    assert refused is not None and refused.code == "programme_too_large"
    (published,) = await repo.list_programmes(key="heavy")
    assert len(json.dumps(published).encode()) <= 16 * 1024


async def test_concurrent_creations_never_pass_the_live_cap(factory) -> None:
    """The advisory lock serialises writers: racing for the last slots, only the
    allowed number commits and the losers leave no row."""
    repo = PgProgrammeRepo(factory, max_live=3, max_live_per_coordinator=8)
    owners = [await _project(factory) for _ in range(6)]
    results = await asyncio.gather(
        *(
            repo.upsert_programme(caller=project, key=f"race{index}", title="R")
            for index, project in enumerate(owners)
        ),
        return_exceptions=True,
    )
    committed = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, ProgrammeError)]
    assert len(committed) == 3 and len(refused) == 3, results
    assert {r.code for r in refused} == {"programme_limit_reached"}
    async with factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(programmes))
    assert count == 3


async def test_every_write_signs_the_gate_and_only_transitions_move_last_change(
    factory, coordinator: str, owner: str
) -> None:
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="sig", title="S")
    await repo.upsert_gate(caller=coordinator, programme="sig", gate="G1", title="t", owner=owner)
    moved = await repo.report_gate(caller=owner, programme="sig", gate="G1", state="in_progress")
    change = moved["last_change"]
    stamp = moved["gates"][0]["updated_at"]

    def signed(published: dict, by: str) -> None:
        nonlocal stamp
        gate = published["gates"][0]
        assert gate["updated_by"] == by
        assert gate["updated_at"] > stamp
        stamp = gate["updated_at"]
        assert published["last_change"] == change

    signed(
        await repo.upsert_gate(caller=coordinator, programme="sig", gate="G1", weight=3),
        coordinator,
    )
    signed(await repo.report_gate(caller=owner, programme="sig", gate="G1", note="n"), owner)
    signed(
        await repo.report_gate(
            caller=owner, programme="sig", gate="G1", progress={"done": 1, "total": 2, "unit": "t"}
        ),
        owner,
    )
    signed(await repo.request(caller=owner, programme="sig", gate="G1", ask="go"), owner)
    signed(await repo.request(caller=owner, programme="sig", gate="G1", ask="go"), owner)
    signed(
        await repo.request(caller=coordinator, programme="sig", gate="G1", ask="other"),
        coordinator,
    )
    signed(await repo.request(caller=owner, programme="sig", gate="G1", ask=None), owner)
    created = await repo.upsert_gate(
        caller=coordinator, programme="sig", gate="G2", title="t", owner=owner, state="done"
    )
    assert created["last_change"] == change, "creating a gate is not a transition"
    through_upsert = await repo.upsert_gate(
        caller=coordinator, programme="sig", gate="G1", state="blocked"
    )
    assert through_upsert["last_change"]["to"] == "blocked"
    assert through_upsert["last_change"]["by"] == coordinator


class _ReportsMidRead(PgProgrammeRepo):
    """Commits a gate report after the programme rows were read, before their gates."""

    def __init__(self, factory: async_sessionmaker, owner: str) -> None:
        super().__init__(factory)
        self._writer = PgProgrammeRepo(factory)
        self._owner = owner
        self._reported = False

    async def _published(self, session, row):  # type: ignore[no-untyped-def]
        if not self._reported:
            self._reported = True
            await self._writer.report_gate(
                caller=self._owner, programme=row["key"], gate="G1", state="done"
            )
        return await super()._published(session, row)


@pytest.mark.parametrize("read", ["list_programmes", "published_block"])
async def test_a_read_publishes_one_snapshot_even_when_a_report_lands_mid_read(
    factory, coordinator: str, owner: str, read: str
) -> None:
    """Review round 2 of #377: a programme and its gates come from one snapshot.

    A report committed between the programme read and the gate read must not publish
    a gate already `done` beside the programme's pre-transition `last_change`.
    """
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="snap", title="Snapshot")
    await repo.upsert_gate(caller=coordinator, programme="snap", gate="G1", title="t", owner=owner)

    racing = _ReportsMidRead(factory, owner)
    if read == "list_programmes":
        (published,) = await racing.list_programmes(key="snap")
    else:
        (published,) = (await racing.published_block())["programmes"]

    assert racing._reported
    assert published["last_change"] is None
    assert published["gates"][0]["state"] == "todo"
    # The report did commit: the next read sees both halves of it.
    (after,) = await repo.list_programmes(key="snap")
    assert after["gates"][0]["state"] == "done"
    assert after["last_change"]["to"] == "done"
