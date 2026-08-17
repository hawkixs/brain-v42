"""Behavior tests for the Codex ticket project-group boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.models.ticket import TicketCreate
from brain_v42.services.project_group_ticket_service import ProjectGroupTicketService
from brain_v42.services.ticket_service import NotAllowedError, TicketNotFoundError, TicketService

_METADATA = sa.MetaData()
_PROJECT_CONTEXTS = sa.Table(
    "project_contexts",
    _METADATA,
    sa.Column("project_key", sa.String(50), primary_key=True),
    sa.Column("project_group", sa.String(50)),
)
_TICKETS = sa.Table(
    "tickets",
    _METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("from_project", sa.String(50), nullable=False),
    sa.Column("to_project", sa.String(50), nullable=False),
)


@pytest.fixture
async def ticket_scope_store() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(_METADATA.create_all)
        await connection.execute(
            _PROJECT_CONTEXTS.insert(),
            [
                {"project_key": "red", "project_group": "red"},
                {"project_key": "red-writer", "project_group": "red"},
                {"project_key": "outside-a", "project_group": "other"},
                {"project_key": "outside-b", "project_group": "other"},
            ],
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_ticket(
    factory: async_sessionmaker[AsyncSession],
    *,
    from_project: str,
    to_project: str,
) -> UUID:
    ticket_id = uuid4()
    async with factory.begin() as session:
        await session.execute(
            _TICKETS.insert().values(
                id=ticket_id,
                from_project=from_project,
                to_project=to_project,
            )
        )
    return ticket_id


def _service(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[ProjectGroupTicketService, AsyncMock]:
    canonical = AsyncMock(spec=TicketService)
    return (
        ProjectGroupTicketService(canonical, factory, project_group="red"),
        canonical,
    )


@pytest.mark.asyncio
async def test_reply_hides_outside_ticket_from_an_in_group_actor(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="outside-a",
        to_project="outside-b",
    )
    service, _ = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.reply(ticket_id, "red:operator", "must stay hidden")


@pytest.mark.asyncio
async def test_create_rejects_an_outside_creator(ticket_scope_store) -> None:
    service, _ = _service(ticket_scope_store)
    payload = TicketCreate(
        kind="request",
        title="outside create",
        body="must be rejected",
        from_project="outside-a",
        to_project="red-writer",
    )

    with pytest.raises(NotAllowedError, match="project group 'red'"):
        await service.create(payload)


@pytest.mark.asyncio
async def test_create_rejects_recursive_descendant_of_a_colonized_group_base(
    ticket_scope_store,
) -> None:
    colonized_base = "scope:red"
    recursive_descendant = f"{colonized_base}:worker"
    async with ticket_scope_store.begin() as session:
        await session.execute(
            _PROJECT_CONTEXTS.insert(),
            [
                {"project_key": colonized_base, "project_group": "red"},
                {"project_key": recursive_descendant, "project_group": None},
            ],
        )
    service, _ = _service(ticket_scope_store)
    payload = TicketCreate(
        kind="request",
        title="recursive descendant create",
        body="must be rejected",
        from_project=recursive_descendant,
        to_project=colonized_base,
    )

    with pytest.raises(NotAllowedError, match="project group 'red'"):
        await service.create(payload)


@pytest.mark.asyncio
async def test_create_delegates_for_an_in_group_creator(ticket_scope_store) -> None:
    service, canonical = _service(ticket_scope_store)
    created = object()
    canonical.create.return_value = created
    payload = TicketCreate(
        kind="request",
        title="red create",
        body="must be delegated",
        from_project="red-writer",
        to_project="outside-a",
    )

    assert await service.create(payload) is created


@pytest.mark.asyncio
async def test_create_delegates_when_the_creator_is_a_colon_child_of_a_group_member(
    ticket_scope_store,
) -> None:
    service, canonical = _service(ticket_scope_store)
    created = object()
    canonical.create.return_value = created
    payload = TicketCreate(
        kind="request",
        title="child create",
        body="must inherit the base group",
        from_project="red:operator",
        to_project="outside-a",
    )

    assert await service.create(payload) is created
    canonical.create.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_create_rejects_a_prefix_lookalike_that_is_not_a_colon_child(
    ticket_scope_store,
) -> None:
    async with ticket_scope_store.begin() as session:
        await session.execute(
            _PROJECT_CONTEXTS.insert().values(
                project_key="red-writerx",
                project_group="other",
            )
        )
    service, canonical = _service(ticket_scope_store)
    payload = TicketCreate(
        kind="request",
        title="prefix lookalike",
        body="must not inherit the group",
        from_project="red-writerx",
        to_project="red-writer",
    )

    with pytest.raises(NotAllowedError):
        await service.create(payload)

    canonical.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_accepts_a_registered_subpartition_via_its_group_base(
    ticket_scope_store,
) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red:operator",
        to_project="outside-a",
    )
    service, canonical = _service(ticket_scope_store)
    reply = object()
    canonical.reply.return_value = reply

    assert await service.reply(ticket_id, "red:operator", "in scope") is reply
    canonical.reply.assert_awaited_once_with(ticket_id, "red:operator", "in scope")


@pytest.mark.asyncio
async def test_reply_hides_an_in_group_ticket_from_an_outside_actor(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="outside-a",
    )
    service, _ = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.reply(ticket_id, "outside-b", "must stay hidden")


@pytest.mark.asyncio
async def test_reply_hides_an_unknown_ticket(ticket_scope_store) -> None:
    service, _ = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.reply(uuid4(), "red-writer", "missing")


@pytest.mark.asyncio
async def test_transition_delegates_for_an_in_group_participant(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="outside-a",
    )
    service, canonical = _service(ticket_scope_store)
    transitioned = object()
    canonical.transition.return_value = transitioned

    result = await service.transition(ticket_id, "red-writer", "start", "taking it")

    canonical.transition.assert_awaited_once_with(
        ticket_id,
        "red-writer",
        "start",
        "taking it",
    )
    assert result is transitioned


@pytest.mark.asyncio
async def test_transition_defaults_its_message_to_none(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="red-writer",
    )
    service, canonical = _service(ticket_scope_store)

    await service.transition(ticket_id, "red-writer", "start")

    canonical.transition.assert_awaited_once_with(ticket_id, "red-writer", "start", None)


@pytest.mark.asyncio
async def test_transition_hides_a_ticket_from_an_out_of_group_author(
    ticket_scope_store,
) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="outside-a",
    )
    service, canonical = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.transition(ticket_id, "outside-a", "resolve")

    canonical.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_transition_reports_not_found_for_an_unknown_ticket(ticket_scope_store) -> None:
    service, canonical = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.transition(uuid4(), "red-writer", "resolve")

    canonical.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_with_thread_hides_an_outside_ticket(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="outside-a",
        to_project="outside-b",
    )
    service, _ = _service(ticket_scope_store)

    assert await service.get_with_thread(ticket_id) is None


@pytest.mark.asyncio
async def test_get_with_thread_delegates_for_an_in_group_ticket(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="outside-a",
    )
    service, canonical = _service(ticket_scope_store)
    ticket_with_thread = object()
    canonical.get_with_thread.return_value = ticket_with_thread

    assert await service.get_with_thread(ticket_id) is ticket_with_thread


@pytest.mark.asyncio
async def test_get_with_thread_delegates_when_only_the_recipient_is_in_group(
    ticket_scope_store,
) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="outside-a",
        to_project="red-writer",
    )
    service, canonical = _service(ticket_scope_store)

    await service.get_with_thread(ticket_id)

    canonical.get_with_thread.assert_awaited_once_with(ticket_id)


@pytest.mark.asyncio
async def test_get_with_thread_returns_none_for_an_unknown_ticket(ticket_scope_store) -> None:
    service, canonical = _service(ticket_scope_store)

    assert await service.get_with_thread(uuid4()) is None
    canonical.get_with_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_with_thread_returns_none_for_a_prefix_lookalike_participant(
    ticket_scope_store,
) -> None:
    async with ticket_scope_store.begin() as session:
        await session.execute(
            _PROJECT_CONTEXTS.insert().values(
                project_key="red-writerx",
                project_group="other",
            )
        )
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writerx",
        to_project="red-writerx",
    )
    service, canonical = _service(ticket_scope_store)

    assert await service.get_with_thread(ticket_id) is None
    canonical.get_with_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_inner_service_errors_propagate_through_the_fence(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="red-writer",
        to_project="red-writer",
    )
    service, canonical = _service(ticket_scope_store)
    canonical.transition.side_effect = NotAllowedError("illegal transition")

    with pytest.raises(NotAllowedError, match="illegal transition"):
        await service.transition(ticket_id, "red-writer", "confirm")
