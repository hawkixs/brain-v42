"""Archiving a project takes it out of the default views and deletes nothing.

The operator wants projects off the DISK while their knowledge stays in the
brain. That verb did not exist: `project_contexts` carries 29 columns and not
one of them is a lifecycle. `current_phase` is free prose — one project already
says "télémétrie historique — pas de développement actif" in it, which no query
can read — and `project_group` is a grouping, so reusing it would destroy the
real group.

Two things are load-bearing here and both are pinned below:

* archiving writes the marker and NOTHING ELSE. `brain_set_project_context` is
  not a PATCH: routing through it would overwrite focus, blockers, group and
  metadata, which is the trap ticket 44ee7643 already paid for once.
* archiving is not deleting. Not a learning, not a plan, not the
  `project_contexts` row. The brain has to survive the repository disappearing
  from disk without losing a line.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from tests.unit.repositories.test_pg_project_context import (
    _make_mock_session,
    _make_row,
    _patch_factory,
)


def _compiled(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _repo():
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo

    return PgProjectContextRepo()


class TestTheMarkerIsStructured:
    def test_project_contexts_carries_an_archival_marker(self) -> None:
        """A queryable column, not prose in `current_phase`."""
        from brain_v42.db.tables import project_contexts

        assert "archived_at" in project_contexts.c
        assert "archived_reason" in project_contexts.c

    def test_the_marker_is_nullable_so_active_is_the_absence_of_a_mark(self) -> None:
        """No backfill, no default: every existing project stays active, and
        `archived_at IS NULL` is the whole definition of active."""
        from brain_v42.db.tables import project_contexts

        assert project_contexts.c.archived_at.nullable is True
        assert project_contexts.c.archived_at.default is None
        assert project_contexts.c.archived_at.server_default is None

    def test_the_model_exposes_it_too(self) -> None:
        """A column the model cannot see is a column no tool can report."""
        from brain_v42.models.project_context import ProjectContext

        assert "archived_at" in ProjectContext.model_fields
        assert "archived_reason" in ProjectContext.model_fields


class TestArchiveWritesTheMarkerAndNothingElse:
    @pytest.mark.asyncio
    async def test_archive_touches_three_columns(self) -> None:
        """Every other column is why this is not `brain_set_project_context`."""
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = _make_row(
            "red-quant", archived_at=datetime.now(UTC), archived_reason="dead on disk"
        )

        with _patch_factory(session):
            await _repo().archive("red-quant", reason="dead on disk")

        statement = session.execute.await_args_list[0].args[0]
        sql = _compiled(statement)
        assert sql.startswith("UPDATE project_contexts SET")
        # Assignment targets only: splitting on commas would also catch the one
        # inside coalesce(...), which is an argument and not a column being set.
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assigned = set(re.findall(r"(?:^|, )(\w+)=", set_clause.strip()))
        assert assigned == {"archived_at", "archived_reason", "updated_at"}

    @pytest.mark.asyncio
    async def test_archive_never_touches_focus_blockers_group_or_metadata(self) -> None:
        """The four fields `brain_set_project_context` silently overwrites."""
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = _make_row(
            "red-quant", archived_at=datetime.now(UTC)
        )

        with _patch_factory(session):
            await _repo().archive("red-quant", reason="dead on disk")

        sql = _compiled(session.execute.await_args_list[0].args[0])
        for column in ("current_focus", "blockers", "project_group", "metadata"):
            assert column not in sql.split("WHERE", 1)[0]

    @pytest.mark.asyncio
    async def test_archiving_is_never_a_delete(self) -> None:
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = _make_row(
            "red-quant", archived_at=datetime.now(UTC)
        )

        with _patch_factory(session):
            await _repo().archive("red-quant", reason="dead on disk")

        for call in session.execute.await_args_list:
            sql = _compiled(call.args[0]).upper()
            assert "DELETE" not in sql
            assert "TRUNCATE" not in sql

    @pytest.mark.asyncio
    async def test_an_unknown_project_returns_none_rather_than_inventing_one(self) -> None:
        """Archiving a typo must not create a project context."""
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = None

        with _patch_factory(session):
            result = await _repo().archive("no-such-project", reason="x")

        assert result is None
        sql = _compiled(session.execute.await_args_list[0].args[0]).upper()
        assert "INSERT" not in sql


class TestUnarchiveClearsTheMark:
    @pytest.mark.asyncio
    async def test_unarchive_clears_both_columns(self) -> None:
        """A reason surviving the unarchive would describe a state that ended."""
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = _make_row(
            "red-quant"
        )

        with _patch_factory(session):
            await _repo().unarchive("red-quant")

        sql = _compiled(session.execute.await_args_list[0].args[0])
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assert "archived_at=NULL" in set_clause.replace(" ", "")
        assert "archived_reason=NULL" in set_clause.replace(" ", "")


class TestArchivingTwiceIsANoOp:
    @pytest.mark.asyncio
    async def test_the_date_records_when_it_was_archived_not_when_asked_again(self) -> None:
        """`archived_at` is a fact about the project, not about the last command.

        Moving it on every call would make "archived since" mean "last time
        somebody re-ran this", and the column would quietly stop answering the
        only question it exists for.
        """
        session = _make_mock_session()
        session.execute.return_value.mappings.return_value.first.return_value = _make_row(
            "red-quant", archived_at=datetime.now(UTC)
        )

        with _patch_factory(session):
            await _repo().archive("red-quant", reason="dead on disk")

        sql = _compiled(session.execute.await_args_list[0].args[0])
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assert "coalesce" in set_clause.lower(), (
            "the date must be kept when already archived, not overwritten"
        )
        assert "project_contexts.archived_at" in set_clause, (
            "the coalesce must read the existing value, not a bind"
        )
