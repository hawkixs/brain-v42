"""Point-of-use project scoping for knowledge and indexed-plan repositories."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from inspect import signature
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from brain_v42.models.adr import ADRUpdate
from brain_v42.models.decision import DecisionUpdate
from brain_v42.models.learning import LearningUpdate
from brain_v42.models.runbook import RunbookUpdate
from brain_v42.models.snippet import SnippetUpdate
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo

ENTITY_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNED_REFERENCE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
FOREIGN_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROJECT_KEY = "sec1b-owned"


def _result(
    *,
    row: Any = None,
    rows: list[dict[str, Any]] | None = None,
    scalar: Any = None,
) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.first.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    result.one_or_none.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    result.rowcount = 1 if scalar is not None else 0
    return result


def _session(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=list(results) if results else None,
        return_value=None if results else _result(),
    )
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _compiled_sql(call: Any) -> str:
    statement = call.args[0]
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).lower().split())


def _assert_project_key_parameter(method: Any) -> None:
    assert "project_key" in signature(method).parameters


REPO_CASES = (
    (PgDecisionRepo, "decisions", DecisionUpdate(title="scoped")),
    (PgLearningRepo, "learnings", LearningUpdate(topic="scoped")),
    (PgSnippetRepo, "snippets", SnippetUpdate(title="scoped")),
    (PgRunbookRepo, "runbooks", RunbookUpdate(title="scoped")),
    (PgADRRepo, "adrs", ADRUpdate(title="scoped")),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("repo_type", "table", "update"), REPO_CASES)
async def test_scoped_get_predicates_id_and_project_at_the_select(
    repo_type: type[Any], table: str, update: Any
) -> None:
    del update
    _assert_project_key_parameter(repo_type.get_by_id)
    session = _session()

    result = await repo_type().get_by_id(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert result is None
    assert session.execute.await_count == 1
    sql = _compiled_sql(session.execute.await_args)
    assert f"from {table}" in sql
    assert f"{table}.id =" in sql
    assert f"{table}.project_key =" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(("repo_type", "table", "update"), REPO_CASES)
async def test_scoped_update_predicates_id_and_project_in_one_statement(
    repo_type: type[Any], table: str, update: Any
) -> None:
    _assert_project_key_parameter(repo_type.update)
    session = _session()

    result = await repo_type().update(
        ENTITY_ID,
        update,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert result is None
    assert session.execute.await_count == 1
    sql = _compiled_sql(session.execute.await_args)
    assert sql.startswith(f"update {table}")
    assert f"{table}.id =" in sql
    assert f"{table}.project_key =" in sql


NON_DECISION_DELETE_CASES = (
    (PgLearningRepo, "learnings"),
    (PgSnippetRepo, "snippets"),
    (PgRunbookRepo, "runbooks"),
    (PgADRRepo, "adrs"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("repo_type", "table"), NON_DECISION_DELETE_CASES)
async def test_scoped_delete_predicates_id_and_project_in_one_statement(
    repo_type: type[Any], table: str
) -> None:
    _assert_project_key_parameter(repo_type.delete)
    session = _session()

    result = await repo_type().delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert result is False
    assert session.execute.await_count == 1
    sql = _compiled_sql(session.execute.await_args)
    assert sql.startswith(f"delete from {table}")
    assert f"{table}.id =" in sql
    assert f"{table}.project_key =" in sql


NO_OP_CASES = (
    (PgDecisionRepo, DecisionUpdate()),
    (PgLearningRepo, LearningUpdate()),
    (PgSnippetRepo, SnippetUpdate()),
    (PgRunbookRepo, RunbookUpdate()),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("repo_type", "update"), NO_OP_CASES)
async def test_scoped_no_op_update_refetches_with_the_same_project(
    repo_type: type[Any], update: Any
) -> None:
    _assert_project_key_parameter(repo_type.update)
    repo = repo_type()
    repo.get_by_id = AsyncMock(return_value=None)
    session = _session()

    result = await repo.update(
        ENTITY_ID,
        update,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert result is None
    repo.get_by_id.assert_awaited_once_with(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_adr_empty_update_still_carries_the_project_predicate() -> None:
    _assert_project_key_parameter(PgADRRepo.update)
    session = _session()

    result = await PgADRRepo().update(
        ENTITY_ID,
        ADRUpdate(),
        project_key=PROJECT_KEY,
        session=session,
    )

    assert result is None
    sql = _compiled_sql(session.execute.await_args)
    assert "adrs.id =" in sql
    assert "adrs.project_key =" in sql


@pytest.mark.asyncio
async def test_scoped_decision_delete_foreign_target_stops_after_locked_lookup() -> None:
    _assert_project_key_parameter(PgDecisionRepo.delete)
    session = _session(_result(scalar=None))

    deleted = await PgDecisionRepo().delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert deleted is False
    assert session.execute.await_count == 1
    sql = _compiled_sql(session.execute.await_args_list[0])
    assert "decisions.id =" in sql
    assert "decisions.project_key =" in sql
    assert "for update" in sql
    session.begin_nested.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_project_key", ["sec1b-foreign", None])
async def test_scoped_decision_delete_foreign_reference_stops_before_mutation(
    foreign_project_key: str | None,
) -> None:
    session = _session(
        _result(scalar=ENTITY_ID),
        _result(
            rows=[
                {"id": OWNED_REFERENCE_ID, "project_key": PROJECT_KEY},
                {"id": FOREIGN_ID, "project_key": foreign_project_key},
            ],
            scalar=FOREIGN_ID,
        ),
    )

    deleted = await PgDecisionRepo().delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert deleted is False
    assert session.execute.await_count == 2
    target_lock_sql, reference_locks_sql = (
        _compiled_sql(call) for call in session.execute.await_args_list
    )
    assert "for update" in target_lock_sql
    assert "select decisions.id, decisions.project_key" in reference_locks_sql
    assert "decisions.superseded_by =" in reference_locks_sql
    assert "for update" in reference_locks_sql
    assert "is distinct from" not in reference_locks_sql
    assert "limit" not in reference_locks_sql
    session.begin_nested.assert_called_once_with()


@pytest.mark.asyncio
async def test_scoped_decision_delete_keeps_lock_clear_and_delete_in_one_transaction() -> None:
    _assert_project_key_parameter(PgDecisionRepo.delete)
    session = _session(
        _result(scalar=ENTITY_ID),
        _result(
            rows=[{"id": OWNED_REFERENCE_ID, "project_key": PROJECT_KEY}],
        ),
        _result(),
        _result(scalar=ENTITY_ID),
    )

    deleted = await PgDecisionRepo().delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
        session=session,
    )

    assert deleted is True
    assert session.execute.await_count == 4
    lock_sql, reference_locks_sql, clear_sql, delete_sql = (
        _compiled_sql(call) for call in session.execute.await_args_list
    )
    assert "for update" in lock_sql
    assert "select decisions.id, decisions.project_key" in reference_locks_sql
    assert "decisions.superseded_by =" in reference_locks_sql
    assert "for update" in reference_locks_sql
    assert "is distinct from" not in reference_locks_sql
    assert "limit" not in reference_locks_sql
    assert "decisions.superseded_by =" in clear_sql
    assert "decisions.project_key =" in clear_sql
    assert "decisions.id =" in delete_sql
    assert "decisions.project_key =" in delete_sql
    session.begin_nested.assert_called_once_with()


def _plan_row() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": ENTITY_ID,
        "file_path": "docs/plans/sec1b.md",
        "title": "SEC1b",
        "plan_type": "plan",
        "project_key": PROJECT_KEY,
        "content_hash": "a" * 64,
        "content": "# SEC1b",
        "summary": None,
        "status": "active",
        "tags": [],
        "metadata": {},
        "chunk_count": 0,
        "word_count": 1,
        "access_count": 0,
        "last_accessed_at": None,
        "freshness_status": "fresh",
        "indexed_at": now,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_scoped_plan_get_predicates_parent_and_chunk_queries() -> None:
    _assert_project_key_parameter(PgIndexedPlanRepo.get_with_chunks)
    parent_result = _result(row=_plan_row())
    chunks_result = _result()
    session = _session(parent_result, chunks_result)

    result = await PgIndexedPlanRepo(session).get_with_chunks(
        ENTITY_ID,
        project_key=PROJECT_KEY,
    )

    assert result is not None
    assert result[0].project_key == PROJECT_KEY
    assert result[1] == []
    parent_call, chunks_call = session.execute.await_args_list
    assert "id = :id and project_key = :project_key" in str(parent_call.args[0]).lower()
    assert "plan_id = :id and project_key = :project_key" in str(chunks_call.args[0]).lower()
    assert parent_call.args[1] == {"id": ENTITY_ID, "project_key": PROJECT_KEY}
    assert chunks_call.args[1] == {"id": ENTITY_ID, "project_key": PROJECT_KEY}


@pytest.mark.asyncio
async def test_scoped_plan_delete_predicates_id_and_project() -> None:
    _assert_project_key_parameter(PgIndexedPlanRepo.delete)
    session = _session(
        _result(scalar=ENTITY_ID),
        _result(
            rows=[{"id": OWNED_REFERENCE_ID, "project_key": PROJECT_KEY}],
        ),
        _result(scalar=ENTITY_ID),
    )

    deleted = await PgIndexedPlanRepo(session).delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
    )

    assert deleted is True
    assert session.execute.await_count == 3
    lock_call, chunk_locks_call, delete_call = session.execute.await_args_list
    assert "id = :id and project_key = :project_key" in str(lock_call.args[0]).lower()
    assert "for update" in str(lock_call.args[0]).lower()
    chunk_locks_sql = str(chunk_locks_call.args[0]).lower()
    assert chunk_locks_sql == (
        "select id, project_key from indexed_plan_chunks where plan_id = :id for update"
    )
    assert "delete from indexed_plans" in str(delete_call.args[0]).lower()
    for call in (lock_call, delete_call):
        assert call.args[1] == {"id": ENTITY_ID, "project_key": PROJECT_KEY}
    assert chunk_locks_call.args[1] == {"id": ENTITY_ID}
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_project_key", ["sec1b-foreign", None])
async def test_scoped_plan_delete_foreign_chunk_rolls_back_without_delete(
    foreign_project_key: str | None,
) -> None:
    session = _session(
        _result(scalar=ENTITY_ID),
        _result(
            rows=[
                {"id": OWNED_REFERENCE_ID, "project_key": PROJECT_KEY},
                {"id": FOREIGN_ID, "project_key": foreign_project_key},
            ],
            scalar=FOREIGN_ID,
        ),
    )

    deleted = await PgIndexedPlanRepo(session).delete(
        ENTITY_ID,
        project_key=PROJECT_KEY,
    )

    assert deleted is False
    assert session.execute.await_count == 2
    lock_call, chunk_locks_call = session.execute.await_args_list
    assert "for update" in str(lock_call.args[0]).lower()
    assert str(chunk_locks_call.args[0]).lower() == (
        "select id, project_key from indexed_plan_chunks where plan_id = :id for update"
    )
    assert chunk_locks_call.args[1] == {"id": ENTITY_ID}
    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_plan_delete_rolls_back_on_query_exception() -> None:
    session = _session()
    session.execute = AsyncMock(side_effect=RuntimeError("query failed"))

    with pytest.raises(RuntimeError, match="query failed"):
        await PgIndexedPlanRepo(session).delete(
            ENTITY_ID,
            project_key=PROJECT_KEY,
        )

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_plan_queries_remain_byte_for_byte_unscoped() -> None:
    session = _session(_result(row=None), _result(scalar=None))
    repo = PgIndexedPlanRepo(session)

    assert await repo.get_with_chunks(ENTITY_ID) is None
    assert await repo.delete(ENTITY_ID) is False

    get_call, delete_call = session.execute.await_args_list
    assert str(get_call.args[0]) == "SELECT * FROM indexed_plans WHERE id = :id"
    assert get_call.args[1] == {"id": ENTITY_ID}
    assert str(delete_call.args[0]) == "DELETE FROM indexed_plans WHERE id = :id"
    assert delete_call.args[1] == {"id": ENTITY_ID}
