"""Unit contracts for atomic compare-and-swap ticket transitions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.models.ticket import ExtractionStatus, TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo


def _ticket_row(
    ticket_id: UUID,
    *,
    status: TicketStatus = TicketStatus.RESOLVED,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": ticket_id,
        "kind": TicketKind.REQUEST.value,
        "title": "atomic transition",
        "body": "body",
        "from_project": "red-shrik",
        "to_project": "red-data",
        "status": status.value,
        "extraction_status": None,
        "resolved_at": now if status is TicketStatus.RESOLVED else None,
        "closed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _result(row: dict[str, Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    return result


def _one_result(row: dict[str, Any]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one.return_value = row
    return result


def _first_result(row: dict[str, Any] | None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    return result


def _all_result(rows: list[dict[str, Any]]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _repo_with_session(session: AsyncMock) -> PgTicketRepo:
    @asynccontextmanager
    async def _session_context():
        yield session

    factory = MagicMock(side_effect=_session_context)
    return PgTicketRepo(factory)


def _session(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


class TestApplyTransition:
    async def test_update_is_compare_and_swap_on_id_and_expected_status(self) -> None:
        ticket_id = uuid4()
        session = _session(_result(_ticket_row(ticket_id)))
        repo = _repo_with_session(session)

        result = await repo.apply_transition(
            ticket_id,
            TicketStatus.RESOLVED,
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )

        assert result is not None
        statement = session.execute.await_args_list[0].args[0]
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        where = sql.split(" WHERE ", maxsplit=1)[1].split(" RETURNING", maxsplit=1)[0]
        assert "tickets.id" in where
        assert "tickets.status" in where
        assert "'open'" in where
        assert "updated_at" not in where

    async def test_status_and_message_share_one_transaction_update_first(self) -> None:
        ticket_id = uuid4()
        session = _session(_result(_ticket_row(ticket_id)), _result())
        repo = _repo_with_session(session)

        await repo.apply_transition(
            ticket_id,
            TicketStatus.RESOLVED,
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=ExtractionStatus.SKIPPED,
            message_author="red-data",
            message_body="done",
        )

        session.begin.assert_called_once_with()
        assert session.execute.await_count == 2
        first = session.execute.await_args_list[0].args[0]
        second = session.execute.await_args_list[1].args[0]
        assert isinstance(first, sa.sql.dml.Update)
        assert isinstance(second, sa.sql.dml.Insert)
        second_sql = str(second.compile(compile_kwargs={"literal_binds": True}))
        assert "ticket_messages" in second_sql
        assert "'red-data'" in second_sql
        assert "'done'" in second_sql
        assert "'resolved'" in second_sql

    async def test_compare_and_swap_miss_returns_none_without_message_insert(self) -> None:
        session = _session(_result(None))
        repo = _repo_with_session(session)

        result = await repo.apply_transition(
            uuid4(),
            TicketStatus.CLOSED,
            expected_status=TicketStatus.OPEN,
            resolved_at=None,
            closed_at=datetime.now(UTC),
            extraction_status=ExtractionStatus.PENDING,
            message_author="red-shrik",
            message_body="cancelled",
        )

        assert result is None
        session.begin.assert_called_once_with()
        session.execute.assert_awaited_once()


class TestAddMessage:
    async def test_reply_is_insert_then_activity_bump_without_status_cas(self) -> None:
        ticket_id = uuid4()
        now = datetime.now(UTC)
        session = _session(
            _one_result(
                {
                    "id": uuid4(),
                    "ticket_id": ticket_id,
                    "author_project": "red-data",
                    "body": "independent reply",
                    "status_to": None,
                    "created_at": now,
                }
            ),
            _result(),
        )
        repo = _repo_with_session(session)

        message = await repo.add_message(ticket_id, "red-data", "independent reply")

        assert message.ticket_id == ticket_id
        assert session.execute.await_count == 2
        insert_statement = session.execute.await_args_list[0].args[0]
        activity_statement = session.execute.await_args_list[1].args[0]
        assert isinstance(insert_statement, sa.sql.dml.Insert)
        assert isinstance(activity_statement, sa.sql.dml.Update)

        insert_sql = str(insert_statement.compile(compile_kwargs={"literal_binds": True}))
        assert "INSERT INTO ticket_messages" in insert_sql

        activity_sql = str(activity_statement.compile(compile_kwargs={"literal_binds": True}))
        set_clause = activity_sql.split(" SET ", maxsplit=1)[1].split(" WHERE ", maxsplit=1)[0]
        where_clause = activity_sql.split(" WHERE ", maxsplit=1)[1]
        assert "updated_at=now()" in set_clause
        assert "status" not in set_clause
        assert "tickets.id" in where_clause
        assert ticket_id.hex in where_clause
        assert "tickets.status" not in where_clause


@pytest.mark.parametrize(
    ("message_author", "message_body"),
    [("red-data", None), (None, "done")],
)
async def test_apply_transition_rejects_partial_message_before_opening_session(
    message_author: str | None,
    message_body: str | None,
) -> None:
    factory = MagicMock(side_effect=AssertionError("session opened"))
    repo = PgTicketRepo(factory)

    with pytest.raises(
        ValueError,
        match="message_author and message_body must be provided together",
    ):
        await repo.apply_transition(
            uuid4(),
            TicketStatus.RESOLVED,
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
            message_author=message_author,
            message_body=message_body,
        )

    factory.assert_not_called()


async def test_create_serializes_domain_values_and_returns_created_ticket() -> None:
    ticket_id = uuid4()
    row = _ticket_row(ticket_id, status=TicketStatus.OPEN)
    row["extraction_status"] = ExtractionStatus.SKIPPED.value
    session = _session(_one_result(row))
    repo = _repo_with_session(session)
    data = TicketCreate(
        kind=TicketKind.REQUEST,
        title="atomic transition",
        body="body",
        from_project="red-shrik",
        to_project="red-data",
        extraction_status=ExtractionStatus.SKIPPED,
    )

    created = await repo.create(data)

    assert created.id == ticket_id
    assert created.extraction_status is ExtractionStatus.SKIPPED
    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert params == {
        "kind": "request",
        "title": "atomic transition",
        "body": "body",
        "from_project": "red-shrik",
        "to_project": "red-data",
        "extraction_status": "skipped",
    }


async def test_get_by_id_returns_validated_ticket_for_existing_row() -> None:
    ticket_id = uuid4()
    session = _session(_first_result(_ticket_row(ticket_id)))
    repo = _repo_with_session(session)

    ticket = await repo.get_by_id(ticket_id)

    assert ticket is not None
    assert ticket.id == ticket_id
    assert ticket.status is TicketStatus.RESOLVED
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert f"WHERE tickets.id = '{ticket_id.hex}'" in sql


async def test_get_by_id_returns_none_for_missing_ticket() -> None:
    repo = _repo_with_session(_session(_first_result(None)))

    assert await repo.get_by_id(uuid4()) is None


async def test_get_messages_preserves_query_order_in_validated_models() -> None:
    ticket_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    first_created_at = datetime(2026, 7, 29, 10, tzinfo=UTC)
    second_created_at = datetime(2026, 7, 29, 11, tzinfo=UTC)
    rows = [
        {
            "id": first_id,
            "ticket_id": ticket_id,
            "author_project": "red-shrik",
            "body": "first",
            "status_to": None,
            "created_at": first_created_at,
        },
        {
            "id": second_id,
            "ticket_id": ticket_id,
            "author_project": "red-data",
            "body": "second",
            "status_to": TicketStatus.RESOLVED.value,
            "created_at": second_created_at,
        },
    ]
    session = _session(_all_result(rows))
    repo = _repo_with_session(session)

    messages = await repo.get_messages(ticket_id)

    assert [message.id for message in messages] == [first_id, second_id]
    assert messages[1].status_to is TicketStatus.RESOLVED
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert f"WHERE ticket_messages.ticket_id = '{ticket_id.hex}'" in sql
    assert "ORDER BY ticket_messages.created_at ASC" in sql


async def test_list_grouped_uses_role_specific_status_buckets() -> None:
    actionable_for_target = _ticket_row(uuid4(), status=TicketStatus.OPEN)
    confirmable_for_requester = _ticket_row(uuid4(), status=TicketStatus.RESOLVED)
    actionable_for_requester = _ticket_row(uuid4(), status=TicketStatus.IN_PROGRESS)
    confirmable_for_target = _ticket_row(uuid4(), status=TicketStatus.RESOLVED)
    session = _session(
        _all_result([actionable_for_target]),
        _all_result([confirmable_for_requester]),
        _all_result([actionable_for_requester]),
        _all_result([confirmable_for_target]),
    )
    repo = _repo_with_session(session)

    groups = await repo.list_grouped("red-data")

    assert [ticket.id for ticket in groups.a_traiter] == [actionable_for_target["id"]]
    assert [ticket.id for ticket in groups.a_confirmer] == [confirmable_for_requester["id"]]
    assert [ticket.id for ticket in groups.en_attente] == [actionable_for_requester["id"]]
    assert [ticket.id for ticket in groups.awaiting_requester_confirmation] == [
        confirmable_for_target["id"]
    ]
    statements = [call.args[0] for call in session.execute.await_args_list]
    sql = [
        str(statement.compile(compile_kwargs={"literal_binds": True})) for statement in statements
    ]
    assert "tickets.to_project = 'red-data'" in sql[0]
    assert "'open'" in sql[0] and "'in_progress'" in sql[0]
    assert "tickets.from_project = 'red-data'" in sql[1]
    assert "'resolved'" in sql[1] and "'wontfix'" in sql[1]
    assert "tickets.from_project = 'red-data'" in sql[2]
    assert "'open'" in sql[2] and "'in_progress'" in sql[2]
    assert "tickets.to_project = 'red-data'" in sql[3]
    assert "'resolved'" in sql[3] and "'wontfix'" in sql[3]
    assert "tickets.from_project != tickets.to_project" in sql[3]


class TestListGrouped:
    async def test_actionable_self_ticket_is_only_in_a_traiter(self) -> None:
        ticket_id = uuid4()
        self_ticket = _ticket_row(ticket_id, status=TicketStatus.OPEN)
        session = _session()

        async def execute(statement: sa.Select) -> MagicMock:
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "tickets.to_project = 'brain-v42'" in sql:
                return _all_result([self_ticket])
            if "tickets.from_project = 'brain-v42'" in sql and "'open'" in sql:
                if "tickets.from_project != tickets.to_project" in sql:
                    return _all_result([])
                return _all_result([self_ticket])
            return _all_result([])

        session.execute = AsyncMock(side_effect=execute)
        repo = _repo_with_session(session)

        groups = await repo.list_grouped("brain-v42")

        assert [ticket.id for ticket in groups.a_traiter] == [ticket_id]
        assert groups.en_attente == []

    async def test_each_category_orders_recent_activity_first_with_stable_ties(self) -> None:
        session = _session(_all_result([]), _all_result([]), _all_result([]), _all_result([]))
        repo = _repo_with_session(session)

        await repo.list_grouped("brain-v42")

        assert session.execute.await_count == 4
        for call in session.execute.await_args_list:
            sql = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert (
                "ORDER BY tickets.updated_at DESC, tickets.created_at DESC, tickets.id ASC" in sql
            )

    async def test_resolved_ticket_delivered_by_us_awaits_requester_confirmation(self) -> None:
        ticket_id = uuid4()
        delivered = _ticket_row(ticket_id, status=TicketStatus.RESOLVED)
        session = _session(
            _all_result([]), _all_result([]), _all_result([]), _all_result([delivered])
        )
        repo = _repo_with_session(session)

        groups = await repo.list_grouped("red-data")

        assert [t.id for t in groups.awaiting_requester_confirmation] == [ticket_id]

    async def test_wontfix_ticket_delivered_by_us_awaits_requester_confirmation(self) -> None:
        ticket_id = uuid4()
        delivered = _ticket_row(ticket_id, status=TicketStatus.WONTFIX)
        session = _session(
            _all_result([]), _all_result([]), _all_result([]), _all_result([delivered])
        )
        repo = _repo_with_session(session)

        groups = await repo.list_grouped("red-data")

        assert [t.id for t in groups.awaiting_requester_confirmation] == [ticket_id]

    async def test_self_ticket_resolved_stays_in_a_confirmer_only(self) -> None:
        # Without the from_project != to_project exclusion, a resolved
        # self-ticket would also surface in awaiting_requester_confirmation
        # (double counting).
        ticket_id = uuid4()
        self_ticket = _ticket_row(ticket_id, status=TicketStatus.RESOLVED)
        self_ticket["from_project"] = "brain-v42"
        self_ticket["to_project"] = "brain-v42"
        session = _session()

        async def execute(statement: sa.Select) -> MagicMock:
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "tickets.from_project = 'brain-v42'" in sql and "'resolved'" in sql:
                return _all_result([self_ticket])
            if "tickets.to_project = 'brain-v42'" in sql and "'resolved'" in sql:
                if "tickets.from_project != tickets.to_project" in sql:
                    return _all_result([])
                return _all_result([self_ticket])
            return _all_result([])

        session.execute = AsyncMock(side_effect=execute)
        repo = _repo_with_session(session)

        groups = await repo.list_grouped("brain-v42")

        assert [t.id for t in groups.a_confirmer] == [ticket_id]
        assert groups.awaiting_requester_confirmation == []
