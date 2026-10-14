"""Recognize only the guard's database error without exposing its row identifiers."""

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from brain_v42.db.merge_chain_errors import MergeChainRefusedError, merge_chain_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("adapted", [False, True])
async def test_guard_error_is_mapped_to_content_safe_exception(adapted: bool) -> None:
    original = asyncpg.CheckViolationError(
        "merge_chain_refused: learnings private-child private-root"
    )
    if adapted:
        original = AsyncAdapt_asyncpg_dbapi.IntegrityError(f"{type(original)}: {original}")
        original.sqlstate = "23514"
    error = sa.exc.DBAPIError("UPDATE", None, original)
    with pytest.raises(MergeChainRefusedError, match="^merge_chain_refused$") as refused:
        async with merge_chain_errors():
            raise error
    assert refused.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        asyncpg.CheckViolationError("another_check: private details"),
        asyncpg.DeadlockDetectedError("merge_chain_refused: private details"),
        asyncpg.CheckViolationError("another_check: content mentions merge_chain_refused"),
        asyncpg.CheckViolationError(""),
    ],
)
async def test_other_database_errors_are_preserved(original: Exception) -> None:
    error = sa.exc.DBAPIError("UPDATE", None, original)
    with pytest.raises(sa.exc.DBAPIError) as failed:
        async with merge_chain_errors():
            raise error
    assert failed.value is error
