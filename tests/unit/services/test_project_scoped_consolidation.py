"""SEC1b source-bounded project scoping for consolidation candidates."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from brain_v42.services.consolidation import ConsolidationJob

PROJECT_KEY = "sec1b-owned"


def _result(rows: list[dict]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _job(
    execute_results: list[MagicMock],
) -> tuple[ConsolidationJob, AsyncMock, AsyncMock]:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_results)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    log_repo = AsyncMock()
    log_repo.get_handled_pairs = AsyncMock(return_value=set())
    return ConsolidationJob(session_factory, log_repo), session, log_repo


def _pair(id_a: UUID, id_b: UUID) -> dict:
    return {
        "id_a": id_a,
        "id_b": id_b,
        "similarity": 0.99,
        "title_a": "A",
        "title_b": "B",
    }


@pytest.mark.asyncio
async def test_scoped_pairs_and_handled_history_are_filtered_in_sql_at_source() -> None:
    first, second = UUID(int=1), UUID(int=2)
    handled_result = _result([{"source_id": second, "target_id": first}])
    pairs_result = _result([_pair(first, second)])
    job, session, log_repo = _job([handled_result, pairs_result])

    candidates = await job.find_candidates(
        entity_type="decision",
        project_key=PROJECT_KEY,
    )

    assert candidates == []
    log_repo.get_handled_pairs.assert_not_awaited()
    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == 2
    handled_sql, pair_sql = (str(statement) for statement in statements)
    assert "consolidation_log" in handled_sql
    assert "JOIN decisions AS handled_source" in handled_sql
    assert "JOIN decisions AS handled_target" in handled_sql
    assert "handled_source.project_key =" in handled_sql
    assert "handled_target.project_key =" in handled_sql
    assert "a.project_key =" in pair_sql
    assert "b.project_key =" in pair_sql


@pytest.mark.asyncio
async def test_admin_candidates_use_historical_global_log_repository_exactly() -> None:
    first, second = UUID(int=1), UUID(int=2)
    pairs_result = _result([_pair(first, second)])
    job, session, log_repo = _job([pairs_result])
    log_repo.get_handled_pairs.return_value = {(first, second), (second, first)}

    candidates = await job.find_candidates(entity_type="decision")

    assert candidates == []
    log_repo.get_handled_pairs.assert_awaited_once_with("decision")
    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == 1
    assert "consolidation_log" not in str(statements[0])
    assert "a.project_key =" not in str(statements[0])
    assert "b.project_key =" not in str(statements[0])


def test_project_scope_is_keyword_only_and_defaults_to_admin() -> None:
    find_signature = inspect.signature(ConsolidationJob.find_candidates)
    pairs_signature = inspect.signature(ConsolidationJob._find_pairs)

    assert find_signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert find_signature.parameters["project_key"].default is None
    assert pairs_signature.parameters["project_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert pairs_signature.parameters["project_key"].default is None
