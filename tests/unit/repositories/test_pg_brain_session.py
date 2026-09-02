"""Unit contract tests for the transactional Brain session repository.

The suite uses SQLAlchemy statement-aware mocks.  It deliberately tests the
repository boundary without a live PostgreSQL instance while preserving the
important transaction, locking, idempotency, capture, and CAS guarantees.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

Row = dict[str, Any]
StatementRouter = Callable[[Any], MagicMock]
CAPTURE_TABLES = (
    "decisions",
    "learnings",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)
STARTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def _sql(statement: Any) -> str:
    """Compile a SQLAlchemy statement to normalized PostgreSQL SQL."""
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).lower().split())


def _params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def _result(
    *,
    row: Row | None = None,
    rows: Iterable[Row] = (),
    scalar: Any = None,
) -> MagicMock:
    """Build a complete-enough SQLAlchemy result double."""
    materialized = list(rows)
    result = MagicMock()
    mappings = MagicMock()
    mappings.one.return_value = row
    mappings.one_or_none.return_value = row
    mappings.first.return_value = row
    mappings.all.return_value = materialized
    result.mappings.return_value = mappings
    result.one.return_value = row
    result.one_or_none.return_value = row
    result.first.return_value = row
    result.all.return_value = materialized
    result.scalar.return_value = scalar
    result.scalar_one.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    scalar_values = [item.get("id", item.get("knowledge_id")) for item in materialized]
    result.scalars.return_value.all.return_value = scalar_values
    result.rowcount = 1 if row is not None else 0
    return result


def _session_row(
    *,
    session_id: UUID | None = None,
    project_key: str = "brain-v42",
    client_key: str = "client-a",
    status: str = "open",
    started_focus: str | None = "old focus",
    started_focus_revision: int = 7,
    summary: str | None = None,
    next_focus: str | None = None,
    captured_knowledge_ids: list[UUID] | None = None,
    nothing_to_capture_reason: str | None = None,
    abandonment_reason: str | None = None,
    end_expected_focus_revision: int | None = None,
    focus_outcome: str | None = None,
    focus_at_end: str | None = None,
    focus_revision_at_end: int | None = None,
) -> Row:
    ended_at = STARTED_AT + timedelta(hours=1) if status != "open" else None
    if status == "ended":
        focus_outcome = focus_outcome or "applied"
        focus_at_end = focus_at_end if focus_at_end is not None else next_focus
        focus_revision_at_end = focus_revision_at_end or started_focus_revision + 1
        end_expected_focus_revision = (
            started_focus_revision
            if end_expected_focus_revision is None
            else end_expected_focus_revision
        )
    return {
        "id": session_id or uuid4(),
        "project_key": project_key,
        "client_key": client_key,
        "status": status,
        "started_focus": started_focus,
        "started_focus_revision": started_focus_revision,
        "summary": summary,
        "next_focus": next_focus,
        "captured_knowledge_ids": captured_knowledge_ids or [],
        "nothing_to_capture_reason": nothing_to_capture_reason,
        "abandonment_reason": abandonment_reason,
        "end_expected_focus_revision": end_expected_focus_revision,
        "focus_outcome": focus_outcome,
        "focus_at_end": focus_at_end,
        "focus_revision_at_end": focus_revision_at_end,
        "started_at": STARTED_AT,
        "last_heartbeat_at": ended_at or STARTED_AT,
        "ended_at": ended_at,
        "updated_at": ended_at or STARTED_AT,
    }


def _make_session(
    router: StatementRouter,
) -> tuple[AsyncMock, list[Any], MagicMock, MagicMock]:
    """Return session, captured statements, transaction CM, and factory."""
    statements: list[Any] = []

    async def execute(statement: Any, *args: Any, **kwargs: Any) -> MagicMock:
        statements.append(statement)
        return router(statement)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.commit = AsyncMock()

    transaction_cm = MagicMock()
    transaction_cm.__aenter__ = AsyncMock(return_value=session)
    transaction_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=transaction_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)
    return session, statements, transaction_cm, factory


def _is_insert(statement: Any, table: str) -> bool:
    return isinstance(statement, sa.sql.dml.Insert) and statement.table.name == table


def _is_update(statement: Any, table: str) -> bool:
    return isinstance(statement, sa.sql.dml.Update) and statement.table.name == table


def _start_router(
    insert_rows: list[Row | None],
    *,
    existing_rows: list[Row] | None = None,
    open_counts: list[int] | None = None,
) -> StatementRouter:
    inserts = iter(insert_rows)
    existing = iter(existing_rows or [])
    counts = iter(open_counts or [1] * max(len(insert_rows), 1))

    def route(statement: Any) -> MagicMock:
        sql = _sql(statement)
        if _is_insert(statement, "brain_sessions"):
            return _result(row=next(inserts))
        if "count(" in sql and "brain_sessions" in sql:
            return _result(scalar=next(counts))
        if "from project_contexts" in sql:
            return _result(row={"current_focus": "old focus", "focus_revision": 7})
        if "from brain_session_artifacts" in sql:
            return _result(rows=[])
        if "from brain_sessions" in sql:
            return _result(row=next(existing))
        raise AssertionError(f"Unexpected start statement: {sql}")

    return route


def _terminal_router(
    locked_row: Row | None,
    *,
    updated_row: Row | None = None,
    valid_capture_ids: Iterable[UUID] = (),
    focus_row: Row | None = None,
    current_focus_row: Row | None = None,
    artifact_rows: Iterable[Row] = (),
    remaining_open: int = 0,
    unattributed: int = 0,
) -> StatementRouter:
    valid_ids = list(valid_capture_ids)
    artifacts = list(artifact_rows)

    def route(statement: Any) -> MagicMock:
        sql = _sql(statement)
        if "count(" in sql and "brain_session_artifacts" in sql:
            # The out-of-ledger count: it names the ledger, not `brain_sessions`.
            return _result(scalar=unattributed)
        if "count(" in sql and "brain_sessions" in sql:
            return _result(scalar=remaining_open)
        if _is_update(statement, "project_contexts"):
            return _result(row=focus_row)
        if _is_update(statement, "brain_sessions"):
            return _result(row=updated_row)
        if _is_insert(statement, "project_focus_history"):
            # 050: the applied CAS records the focus it just wrote, in the same
            # transaction. Routed rather than tolerated — the dispatcher exists
            # to refuse a statement nobody expected, and this one is expected.
            return _result()
        if _is_insert(statement, "brain_session_artifacts"):
            return _result(rows=[{"knowledge_id": capture_id} for capture_id in valid_ids])
        if "from brain_session_artifacts" in sql:
            return _result(rows=artifacts)
        if "from brain_sessions" in sql:
            return _result(row=locked_row)
        if "from learnings" in sql:
            rows = [{"id": capture_id} for capture_id in valid_ids]
            return _result(rows=rows)
        if any(table in sql for table in CAPTURE_TABLES):
            return _result(rows=[])
        if "from project_contexts" in sql:
            return _result(row=current_focus_row or focus_row)
        raise AssertionError(f"Unexpected terminal statement: {sql}")

    return route


def _dml(statements: Iterable[Any]) -> list[Any]:
    return [
        statement
        for statement in statements
        if isinstance(statement, (sa.sql.dml.Insert, sa.sql.dml.Update, sa.sql.dml.Delete))
    ]


class TestRepositoryContract:
    def test_repository_and_result_types_are_importable(self) -> None:
        from brain_v42.models.brain_session import (
            BrainSessionAbandonResult,
            BrainSessionEndResult,
            BrainSessionListResult,
            BrainSessionResumeResult,
            BrainSessionStartResult,
        )
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        assert PgBrainSessionRepo is not None
        assert BrainSessionStartResult is not None
        assert BrainSessionResumeResult is not None
        assert BrainSessionListResult is not None
        assert BrainSessionEndResult is not None
        assert BrainSessionAbandonResult is not None

    def test_repository_uses_brain_sessions_table(self) -> None:
        from brain_v42.db.tables import brain_sessions
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        repo = PgBrainSessionRepo(session_factory=MagicMock())

        assert repo.table is brain_sessions

    def test_repository_is_exported_from_package(self) -> None:
        import brain_v42.repositories as repositories
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        assert repositories.PgBrainSessionRepo is PgBrainSessionRepo
        assert "PgBrainSessionRepo" in repositories.__all__


class TestStart:
    @pytest.mark.asyncio
    async def test_start_snapshots_focus_in_one_transaction(self) -> None:
        from brain_v42.models.brain_session import BrainSessionStartResult
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        created = _session_row()
        session, statements, transaction, factory = _make_session(
            _start_router([created], open_counts=[1])
        )

        result = await PgBrainSessionRepo(factory).start("brain-v42", "client-a")

        assert isinstance(result, BrainSessionStartResult)
        assert result.session.started_focus == "old focus"
        assert result.session.started_focus_revision == 7
        assert result.replayed is False
        assert result.open_session_count == 1
        assert result.briefing == ""
        transaction.__aenter__.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once()
        session.commit.assert_not_awaited()
        insert = next(stmt for stmt in statements if _is_insert(stmt, "brain_sessions"))
        insert_sql = _sql(insert)
        assert "started_focus" in insert_sql
        assert "started_focus_revision" in insert_sql

    @pytest.mark.asyncio
    async def test_start_replays_same_project_client_key(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        existing = _session_row()
        _, statements, _, factory = _make_session(
            _start_router([None], existing_rows=[existing], open_counts=[1])
        )

        result = await PgBrainSessionRepo(factory).start("brain-v42", "client-a")

        assert result.replayed is True
        assert result.session.id == existing["id"]
        insert = next(stmt for stmt in statements if _is_insert(stmt, "brain_sessions"))
        insert_sql = _sql(insert)
        assert "on conflict" in insert_sql
        assert "project_key" in insert_sql
        assert "client_key" in insert_sql

    @pytest.mark.asyncio
    async def test_start_rejects_client_key_whose_session_is_terminal(self) -> None:
        from brain_v42.models.brain_session import BrainSessionClientKeyConflictError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        ended = _session_row(
            status="ended",
            summary="done",
            next_focus="next",
            nothing_to_capture_reason="no durable knowledge",
        )
        _, statements, _, factory = _make_session(_start_router([None], existing_rows=[ended]))

        with pytest.raises(BrainSessionClientKeyConflictError):
            await PgBrainSessionRepo(factory).start("brain-v42", "client-a")

        assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]

    @pytest.mark.asyncio
    async def test_start_allows_two_open_sessions_with_different_client_keys(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        first = _session_row(client_key="client-a")
        second = _session_row(client_key="client-b")
        _, statements, _, factory = _make_session(
            _start_router([first, second], open_counts=[1, 2])
        )
        repo = PgBrainSessionRepo(factory)

        first_result = await repo.start("brain-v42", "client-a")
        second_result = await repo.start("brain-v42", "client-b")

        assert first_result.replayed is False
        assert second_result.replayed is False
        assert first_result.session.id != second_result.session.id
        assert second_result.open_session_count == 2
        assert len([stmt for stmt in statements if _is_insert(stmt, "brain_sessions")]) == 2
        assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]


class TestReadOperations:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_session_or_none(self) -> None:
        from brain_v42.models.brain_session import BrainSession
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        row = _session_row()

        def found_router(statement: Any) -> MagicMock:
            return _result(row=row)

        _, statements, _, factory = _make_session(found_router)
        repo = PgBrainSessionRepo(factory)

        found = await repo.get_by_id(row["id"])

        assert isinstance(found, BrainSession)
        assert found.id == row["id"]
        assert not _dml(statements)

        def missing_router(statement: Any) -> MagicMock:
            return _result(row=None)

        _, missing_statements, _, missing_factory = _make_session(missing_router)
        missing = await PgBrainSessionRepo(missing_factory).get_by_id(uuid4())
        assert missing is None
        assert not _dml(missing_statements)

    @pytest.mark.asyncio
    async def test_list_filters_orders_and_paginates(self) -> None:
        from brain_v42.models.brain_session import BrainSessionListResult
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        rows = [_session_row(client_key="client-a"), _session_row(client_key="client-b")]

        def router(statement: Any) -> MagicMock:
            sql = _sql(statement)
            if "count(" in sql:
                return _result(scalar=4)
            if "from brain_session_artifacts" in sql:
                return _result(rows=[])
            return _result(rows=rows)

        _, statements, _, factory = _make_session(router)

        result = await PgBrainSessionRepo(factory).list("brain-v42", "open", limit=2, offset=1)

        assert isinstance(result, BrainSessionListResult)
        assert len(result.sessions) == 2
        assert result.total == 4
        assert result.limit == 2
        assert result.offset == 1
        select_sql = " ".join(_sql(stmt) for stmt in statements)
        assert "project_key" in select_sql
        assert "status" in select_sql
        assert "order by" in select_sql
        assert "started_at desc" in select_sql
        values = [value for stmt in statements for value in _params(stmt).values()]
        assert "brain-v42" in values
        assert "open" in values
        assert not _dml(statements)

    @pytest.mark.asyncio
    async def test_list_all_omits_status_filter(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        rows = [
            _session_row(client_key="client-a"),
            _session_row(
                client_key="client-b",
                status="abandoned",
                abandonment_reason="superseded",
            ),
        ]

        def router(statement: Any) -> MagicMock:
            sql = _sql(statement)
            if "count(" in sql:
                return _result(scalar=2)
            if "from brain_session_artifacts" in sql:
                return _result(rows=[])
            return _result(rows=rows)

        _, statements, _, factory = _make_session(router)

        result = await PgBrainSessionRepo(factory).list("brain-v42", "all", limit=20, offset=0)

        assert result.total == 2
        assert {session.status.value for session in result.sessions} == {
            "open",
            "abandoned",
        }
        values = [value for stmt in statements for value in _params(stmt).values()]
        assert "brain-v42" in values
        assert "all" not in values

    @pytest.mark.asyncio
    async def test_list_rejects_limit_above_public_maximum(self) -> None:
        from brain_v42.models.brain_session import BrainSessionInputError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        _, statements, _, factory = _make_session(lambda statement: _result())

        with pytest.raises(BrainSessionInputError):
            await PgBrainSessionRepo(factory).list(limit=101)

        assert statements == []

    @pytest.mark.asyncio
    async def test_resume_is_read_only_and_returns_current_focus(self) -> None:
        from brain_v42.models.brain_session import BrainSessionResumeResult
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        row = _session_row()
        capture_id = uuid4()

        def router(statement: Any) -> MagicMock:
            sql = _sql(statement)
            if "count(" in sql:
                return _result(scalar=2)
            if "from project_contexts" in sql:
                return _result(row={"current_focus": "live focus", "focus_revision": 9})
            if "from brain_session_artifacts" in sql:
                return _result(
                    rows=[
                        {
                            "knowledge_id": capture_id,
                            "session_id": row["id"],
                            "knowledge_type": "learning",
                            "captured_at": STARTED_AT,
                        }
                    ]
                )
            if "from brain_sessions" in sql:
                return _result(row={**row, "attributed_knowledge_ids": [capture_id]})
            raise AssertionError(f"Unexpected resume statement: {sql}")

        session, statements, _, factory = _make_session(router)

        result = await PgBrainSessionRepo(factory).resume(row["id"], "client-a")

        assert isinstance(result, BrainSessionResumeResult)
        assert result.session.id == row["id"]
        assert result.open_session_count == 2
        assert result.current_focus == "live focus"
        assert result.current_focus_revision == 9
        assert result.session.attributed_knowledge_ids == [capture_id]
        assert result.briefing == ""
        assert not _dml(statements)
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_rejects_terminal_session_without_writing(self) -> None:
        from brain_v42.models.brain_session import BrainSessionStateError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        ended = _session_row(
            status="ended",
            summary="done",
            next_focus="next",
            nothing_to_capture_reason="administrative work",
        )

        def router(statement: Any) -> MagicMock:
            return _result(row=ended)

        _, statements, _, factory = _make_session(router)

        with pytest.raises(BrainSessionStateError):
            await PgBrainSessionRepo(factory).resume(ended["id"], "client-a")

        assert not _dml(statements)

    @pytest.mark.asyncio
    async def test_list_stale_filters_open_sessions_by_last_heartbeat(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        stale = _session_row()

        def router(statement: Any) -> MagicMock:
            sql = _sql(statement)
            if "count(" in sql:
                return _result(scalar=1)
            if "from brain_session_artifacts" in sql:
                return _result(rows=[])
            return _result(rows=[stale])

        _, statements, _, factory = _make_session(router)

        result = await PgBrainSessionRepo(factory).list("brain-v42", "stale", limit=20, offset=0)

        assert result.sessions[0].status.value == "open"
        assert result.sessions[0].is_stale is True
        combined = " ".join(_sql(stmt) for stmt in statements)
        assert "last_heartbeat_at" in combined
        assert "status" in combined
        assert "open" in _params(statements[0]).values()

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_only_the_addressed_open_session(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        refreshed = dict(opened)
        refreshed["last_heartbeat_at"] = datetime.now(UTC)
        refreshed["updated_at"] = refreshed["last_heartbeat_at"]
        _, statements, _, factory = _make_session(_terminal_router(opened, updated_row=refreshed))

        result = await PgBrainSessionRepo(factory).heartbeat(opened["id"], "client-a")

        assert result.session.id == opened["id"]
        assert result.session.is_stale is False
        updates = [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
        assert len(updates) == 1
        assert "last_heartbeat_at" in _sql(updates[0])
        assert not any(_is_update(stmt, "project_contexts") for stmt in statements)

    @pytest.mark.asyncio
    async def test_identity_guard_accepts_unicode_client_keys(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row(client_key="tâche-é")
        refreshed = dict(opened)
        refreshed["last_heartbeat_at"] = datetime.now(UTC)
        refreshed["updated_at"] = refreshed["last_heartbeat_at"]
        _, _, _, factory = _make_session(_terminal_router(opened, updated_row=refreshed))

        result = await PgBrainSessionRepo(factory).heartbeat(opened["id"], "tâche-é")

        assert result.session.client_key == "tâche-é"


class TestEnd:
    @pytest.mark.asyncio
    async def test_end_rejects_wrong_client_identity_before_any_write(self) -> None:
        from brain_v42.models.brain_session import BrainSessionIdentityConflictError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row(client_key="task-a")
        _, statements, _, factory = _make_session(_terminal_router(opened))

        with pytest.raises(BrainSessionIdentityConflictError):
            await PgBrainSessionRepo(factory).end(
                opened["id"],
                "task-b",
                "summary",
                "next",
                7,
                "nothing durable",
            )

        assert not _dml(statements)
        assert not any("from project_contexts" in _sql(stmt) for stmt in statements)

    @pytest.mark.asyncio
    async def test_capture_rejects_artifact_owned_by_another_session(self) -> None:
        from brain_v42.models.brain_session import BrainSessionCaptureConflictError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        capture_id = uuid4()
        _, statements, _, factory = _make_session(
            _terminal_router(
                opened,
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
                artifact_rows=[
                    {
                        "knowledge_id": capture_id,
                        "session_id": uuid4(),
                        "knowledge_type": "learning",
                        "captured_at": STARTED_AT,
                    }
                ],
            )
        )

        with pytest.raises(BrainSessionCaptureConflictError):
            await PgBrainSessionRepo(factory).capture(opened["id"], "client-a", [capture_id])

        assert not _dml(statements)

    @pytest.mark.asyncio
    async def test_capture_inserts_provenance_and_refreshes_presence(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        capture_id = uuid4()
        refreshed = dict(opened)
        refreshed["last_heartbeat_at"] = datetime.now(UTC)
        refreshed["updated_at"] = refreshed["last_heartbeat_at"]
        _, statements, _, factory = _make_session(
            _terminal_router(
                opened,
                updated_row=refreshed,
                valid_capture_ids=[capture_id],
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            )
        )

        result = await PgBrainSessionRepo(factory).capture(opened["id"], "client-a", [capture_id])

        assert result.captured_knowledge_ids == [capture_id]
        assert result.newly_captured_knowledge_ids == [capture_id]
        assert result.replayed is False
        artifact_insert = next(
            stmt for stmt in statements if _is_insert(stmt, "brain_session_artifacts")
        )
        assert "on conflict" in _sql(artifact_insert)
        heartbeat_update = next(stmt for stmt in statements if _is_update(stmt, "brain_sessions"))
        assert "last_heartbeat_at" in _sql(heartbeat_update)

    @pytest.mark.asyncio
    async def test_end_locks_validates_capture_and_cas_updates_atomically(self) -> None:
        from brain_v42.models.brain_session import BrainSessionEndResult
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        capture_id = uuid4()
        opened = _session_row()
        ended = _session_row(
            session_id=opened["id"],
            status="ended",
            summary="implemented lifecycle",
            next_focus="verify MCP tools",
            captured_knowledge_ids=[capture_id],
        )
        focus = {"current_focus": "verify MCP tools", "focus_revision": 8}
        session, statements, transaction, factory = _make_session(
            _terminal_router(
                opened,
                updated_row=ended,
                valid_capture_ids=[capture_id],
                focus_row=focus,
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
                artifact_rows=[
                    {
                        "knowledge_id": capture_id,
                        "session_id": opened["id"],
                        "knowledge_type": "learning",
                        "captured_at": STARTED_AT,
                    }
                ],
                remaining_open=1,
            )
        )

        result = await PgBrainSessionRepo(factory).end(
            opened["id"],
            "client-a",
            "implemented lifecycle",
            "verify MCP tools",
            7,
            None,
        )

        assert isinstance(result, BrainSessionEndResult)
        assert result.session.status == "ended"
        assert result.replayed is False
        assert result.remaining_open_session_count == 1
        assert result.current_focus == "verify MCP tools"
        assert result.current_focus_revision == 8
        assert result.focus_outcome.value == "applied"
        transaction.__aenter__.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once()
        session.commit.assert_not_awaited()

        lock_sql = next(
            _sql(stmt)
            for stmt in statements
            if "from brain_sessions" in _sql(stmt) and "for update" in _sql(stmt)
        )
        assert "for update" in lock_sql

        capture_sql = " ".join(
            _sql(stmt)
            for stmt in statements
            if any(table in _sql(stmt) for table in CAPTURE_TABLES)
        )
        for table in CAPTURE_TABLES:
            assert table in capture_sql
        assert "project_key" in capture_sql
        assert "created_at" in capture_sql
        assert ">=" in capture_sql
        assert capture_sql.count("for key share") == len(CAPTURE_TABLES)

        focus_update = next(stmt for stmt in statements if _is_update(stmt, "project_contexts"))
        focus_sql = _sql(focus_update)
        assert "focus_revision" in focus_sql
        assert "current_focus" in focus_sql
        assert 8 in _params(focus_update).values()
        assert any(_is_update(stmt, "brain_sessions") for stmt in statements)

    @pytest.mark.asyncio
    async def test_end_accepts_explicit_nothing_to_capture_without_capture_query(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        ended = _session_row(
            session_id=opened["id"],
            status="ended",
            summary="reviewed design",
            next_focus="implement tools",
            nothing_to_capture_reason="no durable new knowledge",
        )
        focus = {"current_focus": "implement tools", "focus_revision": 8}
        _, statements, _, factory = _make_session(
            _terminal_router(
                opened,
                updated_row=ended,
                focus_row=focus,
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            )
        )

        result = await PgBrainSessionRepo(factory).end(
            opened["id"],
            "client-a",
            "reviewed design",
            "implement tools",
            7,
            "no durable new knowledge",
        )

        assert result.session.nothing_to_capture_reason == "no durable new knowledge"
        # The discriminant was TIGHTENED by 047, not loosened: `end` now
        # measures the out-of-ledger artifacts, and that count crosses the same
        # six tables. What this witness guards is capture VALIDATION — the
        # locking query, which must not run when nothing is captured. The count
        # is a measure; it is not one.
        capture_selects = [
            stmt
            for stmt in statements
            if isinstance(stmt, sa.sql.selectable.Select)
            and any(table in _sql(stmt) for table in CAPTURE_TABLES)
            and "count(" not in _sql(stmt)
        ]
        assert capture_selects == []

    @pytest.mark.asyncio
    async def test_capture_rejects_knowledge_outside_project_or_session_window(self) -> None:
        from brain_v42.models.brain_session import BrainSessionInputError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        capture_id = uuid4()
        opened = _session_row()
        _, statements, _, factory = _make_session(
            _terminal_router(
                opened,
                valid_capture_ids=[],
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            )
        )

        with pytest.raises(BrainSessionInputError):
            await PgBrainSessionRepo(factory).capture(opened["id"], "client-a", [capture_id])

        validation_sql = " ".join(
            _sql(stmt)
            for stmt in statements
            if any(table in _sql(stmt) for table in CAPTURE_TABLES)
        )
        assert "project_key" in validation_sql
        assert "created_at" in validation_sql
        assert not any(_is_insert(stmt, "brain_session_artifacts") for stmt in statements)
        assert not any(_is_update(stmt, "brain_sessions") for stmt in statements)

    @pytest.mark.asyncio
    async def test_end_closes_and_reports_stale_focus_as_conflict(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        current = {"current_focus": "someone else's focus", "focus_revision": 8}
        ended = _session_row(
            session_id=opened["id"],
            status="ended",
            summary="summary",
            next_focus="my next focus",
            nothing_to_capture_reason="no durable capture",
            end_expected_focus_revision=7,
            focus_outcome="conflict",
            focus_at_end=current["current_focus"],
            focus_revision_at_end=8,
        )
        session, statements, transaction, factory = _make_session(
            _terminal_router(
                opened,
                updated_row=ended,
                current_focus_row=current,
            )
        )

        result = await PgBrainSessionRepo(factory).end(
            opened["id"],
            "client-a",
            "summary",
            "my next focus",
            7,
            "no durable capture",
        )

        assert result.session.status.value == "ended"
        assert result.focus_outcome.value == "conflict"
        assert result.current_focus == "someone else's focus"
        assert result.current_focus_revision == 8
        assert not any(_is_update(stmt, "project_contexts") for stmt in statements)
        assert any(_is_update(stmt, "brain_sessions") for stmt in statements)
        transaction.__aexit__.assert_awaited_once()
        assert transaction.__aexit__.await_args.args[0] is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_exact_terminal_retry_is_idempotent_and_read_only(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        capture_id = uuid4()
        ended = _session_row(
            status="ended",
            summary="summary",
            next_focus="next",
            captured_knowledge_ids=[capture_id],
        )
        current = {"current_focus": "next", "focus_revision": 8}
        _, statements, _, factory = _make_session(
            _terminal_router(ended, current_focus_row=current, remaining_open=0)
        )

        result = await PgBrainSessionRepo(factory).end(
            ended["id"], "client-a", "summary", "next", 7, None
        )

        assert result.replayed is True
        assert result.session.id == ended["id"]
        assert result.current_focus == "next"
        assert result.current_focus_revision == 8
        assert _dml(statements) == []

    @pytest.mark.asyncio
    async def test_capture_exact_retry_is_idempotent(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        capture_id = uuid4()
        opened = _session_row()
        artifact = {
            "knowledge_id": capture_id,
            "session_id": opened["id"],
            "knowledge_type": "learning",
            "captured_at": STARTED_AT,
        }
        _, statements, _, factory = _make_session(
            _terminal_router(
                opened,
                updated_row=opened,
                valid_capture_ids=[capture_id],
                current_focus_row={"current_focus": "old focus", "focus_revision": 7},
                artifact_rows=[artifact],
            )
        )

        result = await PgBrainSessionRepo(factory).capture(
            opened["id"],
            "client-a",
            [capture_id],
        )

        assert result.replayed is True
        assert result.captured_knowledge_ids == [capture_id]
        assert not any(_is_insert(stmt, "brain_session_artifacts") for stmt in statements)

    @pytest.mark.asyncio
    async def test_capture_exact_retry_after_abandon_remains_idempotent(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        capture_id = uuid4()
        abandoned = _session_row(
            status="abandoned",
            abandonment_reason="work intentionally discarded",
        )
        artifact = {
            "knowledge_id": capture_id,
            "session_id": abandoned["id"],
            "knowledge_type": "learning",
            "captured_at": STARTED_AT,
        }
        _, statements, _, factory = _make_session(
            _terminal_router(abandoned, artifact_rows=[artifact])
        )

        result = await PgBrainSessionRepo(factory).capture(
            abandoned["id"],
            "client-a",
            [capture_id],
        )

        assert result.replayed is True
        assert result.session.status.value == "abandoned"
        assert result.session.attributed_knowledge_ids == [capture_id]
        assert result.captured_knowledge_ids == [capture_id]
        assert _dml(statements) == []

    @pytest.mark.asyncio
    async def test_end_exact_retry_allows_current_focus_to_have_been_cleared(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        ended = _session_row(
            status="ended",
            summary="summary",
            next_focus="next",
            nothing_to_capture_reason="no durable knowledge",
        )
        current = {"current_focus": None, "focus_revision": 9}
        _, statements, _, factory = _make_session(
            _terminal_router(ended, current_focus_row=current, remaining_open=0)
        )

        result = await PgBrainSessionRepo(factory).end(
            ended["id"],
            "client-a",
            "summary",
            "next",
            7,
            "no durable knowledge",
        )

        assert result.replayed is True
        assert result.current_focus is None
        assert _dml(statements) == []

    @pytest.mark.asyncio
    async def test_end_changed_terminal_retry_conflicts_without_writing(self) -> None:
        from brain_v42.models.brain_session import BrainSessionTerminalConflictError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        ended = _session_row(
            status="ended",
            summary="original summary",
            next_focus="next",
            nothing_to_capture_reason="no durable capture",
        )
        _, statements, _, factory = _make_session(_terminal_router(ended))

        with pytest.raises(BrainSessionTerminalConflictError):
            await PgBrainSessionRepo(factory).end(
                ended["id"],
                "client-a",
                "changed summary",
                "next",
                7,
                "no durable capture",
            )

        assert _dml(statements) == []

    @pytest.mark.asyncio
    async def test_end_missing_session_raises_not_found(self) -> None:
        from brain_v42.models.brain_session import BrainSessionNotFoundError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        _, statements, _, factory = _make_session(_terminal_router(None))

        with pytest.raises(BrainSessionNotFoundError):
            await PgBrainSessionRepo(factory).end(
                uuid4(), "client-a", "summary", "next", 7, "no durable capture"
            )

        assert _dml(statements) == []


@pytest.mark.asyncio
async def test_repository_rejects_more_than_one_hundred_capture_ids_before_sql() -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(lambda statement: _result())

    with pytest.raises(BrainSessionInputError, match="at most 100"):
        await PgBrainSessionRepo(factory).capture(
            uuid4(),
            "client-a",
            [uuid4() for _ in range(101)],
        )

    assert statements == []


class TestAbandon:
    @pytest.mark.asyncio
    async def test_abandon_locks_and_updates_only_session_in_one_transaction(self) -> None:
        from brain_v42.models.brain_session import BrainSessionAbandonResult
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        opened = _session_row()
        abandoned = _session_row(
            session_id=opened["id"],
            status="abandoned",
            abandonment_reason="work intentionally discarded",
        )
        session, statements, transaction, factory = _make_session(
            _terminal_router(opened, updated_row=abandoned, remaining_open=2)
        )

        result = await PgBrainSessionRepo(factory).abandon(
            opened["id"], "client-a", "work intentionally discarded"
        )

        assert isinstance(result, BrainSessionAbandonResult)
        assert result.session.status == "abandoned"
        assert result.replayed is False
        assert result.remaining_open_session_count == 2
        transaction.__aenter__.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once()
        assert any(
            "for update" in _sql(stmt) for stmt in statements if "from brain_sessions" in _sql(stmt)
        )
        assert any(_is_update(stmt, "brain_sessions") for stmt in statements)
        assert not any(_is_update(stmt, "project_contexts") for stmt in statements)
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_abandon_exact_retry_is_idempotent_and_read_only(self) -> None:
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        abandoned = _session_row(
            status="abandoned",
            abandonment_reason="work intentionally discarded",
        )
        _, statements, _, factory = _make_session(_terminal_router(abandoned, remaining_open=0))

        result = await PgBrainSessionRepo(factory).abandon(
            abandoned["id"], "client-a", "work intentionally discarded"
        )

        assert result.replayed is True
        assert result.session.id == abandoned["id"]
        assert result.remaining_open_session_count == 0
        assert _dml(statements) == []

    @pytest.mark.asyncio
    async def test_abandon_changed_retry_conflicts_without_writing(self) -> None:
        from brain_v42.models.brain_session import BrainSessionTerminalConflictError
        from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

        abandoned = _session_row(
            status="abandoned",
            abandonment_reason="original reason",
        )
        _, statements, _, factory = _make_session(_terminal_router(abandoned))

        with pytest.raises(BrainSessionTerminalConflictError):
            await PgBrainSessionRepo(factory).abandon(abandoned["id"], "client-a", "changed reason")

        assert _dml(statements) == []
