"""Ownership guards lock before mutation and preserve the legacy SQL."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import learnings
from brain_v42.provenance import advance_checked_project, checked_project, get_checked_project
from brain_v42.repositories.checked_project import (
    CheckedProjectMismatch,
    checked_project_clause,
    lock_checked_owner,
)
from brain_v42.repositories.pg_base import BasePgRepository


def test_unset_clause_preserves_sql():
    statement = sa.update(learnings).where(learnings.c.id == uuid4()).values(topic="changed")
    assert str(statement) == str(statement.where(checked_project_clause(learnings.c.project_key)))
    with checked_project("allowed"):
        assert "learnings.project_key =" in str(
            statement.where(checked_project_clause(learnings.c.project_key))
        )
    assert get_checked_project() is None


def test_holder_advances_and_context_resets_on_failure():
    with pytest.raises(RuntimeError), checked_project("first"):
        advance_checked_project("second")
        assert get_checked_project() == "second"
        raise RuntimeError
    assert get_checked_project() is None


@pytest.mark.parametrize("owner", ["allowed", "outside", None])
async def test_owner_lock_compares_under_for_update(owner):
    result = MagicMock()
    result.one_or_none.return_value = (owner,)
    session = AsyncMock()
    session.execute.return_value = result
    with checked_project("allowed"):
        if owner == "allowed":
            await lock_checked_owner(session, learnings, uuid4())
        else:
            with pytest.raises(CheckedProjectMismatch):
                await lock_checked_owner(session, learnings, uuid4())
    assert "FOR UPDATE" in str(session.execute.call_args.args[0])


async def test_unset_owner_never_queries():
    session = AsyncMock()
    await lock_checked_owner(session, learnings, uuid4())
    session.execute.assert_not_awaited()


async def test_missing_owner_preserves_not_found():
    session = AsyncMock()
    session.execute.return_value = MagicMock(one_or_none=lambda: None)
    with checked_project("allowed"):
        await lock_checked_owner(session, learnings, uuid4())


async def test_checked_update_without_project_column_refuses_before_query():
    table = sa.Table("unowned", sa.MetaData(), sa.Column("id", sa.Uuid, primary_key=True))
    session = AsyncMock()
    repo = BasePgRepository()
    repo.table = table
    with checked_project("allowed"), pytest.raises(CheckedProjectMismatch):
        await repo.update(uuid4(), {}, session=session)
    session.execute.assert_not_awaited()
