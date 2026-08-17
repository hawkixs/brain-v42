"""Every writer of `current_focus` must date it, and only on a real change.

`project_contexts.updated_at` moves on any write to the row — counters
included — so it cannot answer "how old is this focus?". Migration 040 adds
`focus_updated_at` to answer exactly that, which only holds if two rules hold
together:

1. every path that can change `current_focus` stamps the column, and
2. no path stamps it when the stored text is unchanged.

Rule 2 is what keeps a copy-forward visible: re-posting yesterday's prose
verbatim, or a blockers-only batch that re-sends the same focus to consume the
CAS token, must leave the age alone. Without it the column would refresh on
every write and become `updated_at` under a more honest-sounding name.

The comparison lives in SQL rather than in Python so it reads the row being
written, inside the same statement — no read-then-write window.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo
from brain_v42.services.roadmap_service import RoadmapService


def _sql(statement: Any) -> str:
    """Render a statement the way PostgreSQL will receive it."""
    return re.sub(r"\s+", " ", str(statement.compile(dialect=postgresql.dialect(as_uuid=True))))


def _assert_conditional_stamp(statement: Any) -> None:
    """The SET clause must stamp only when the stored focus really changes."""
    sql = _sql(statement)

    assert "focus_updated_at=CASE WHEN" in sql.replace(" =", "=").replace("= ", "="), sql
    assert "project_contexts.current_focus IS DISTINCT FROM" in sql, sql
    assert "THEN now()" in sql, sql
    assert "ELSE project_contexts.focus_updated_at" in sql, sql


def _mock_session(*results: Any) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results) or None)
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _row(**overrides: Any) -> dict[str, Any]:
    """The minimum a `ProjectContext` will validate from."""
    return {"project_key": "brain-v42", "name": "B", "description": "d", **overrides}


def _result(mapping: Any = None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.first.return_value = mapping
    result.mappings.return_value.one.return_value = mapping if mapping is not None else _row()
    result.mappings.return_value.one_or_none.return_value = mapping
    result.mappings.return_value.all.return_value = []
    return result


def _set_clause(statement: Any) -> str:
    """The SET clause alone.

    RETURNING lists every column of the table, `focus_updated_at` included, so
    asserting against the whole statement would pass on any write.
    """
    return _sql(statement).split(" RETURNING ")[0]


def _patch_factory(session: AsyncMock) -> Any:
    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any):
        yield session

    factory = MagicMock()
    factory.side_effect = lambda: _cm()
    return patch(
        "brain_v42.repositories.pg_base.get_session_factory",
        return_value=factory,
    )


def _statements(session: AsyncMock) -> list[Any]:
    return [call.args[0] for call in session.execute.await_args_list]


# ---------------------------------------------------------------------------
# PgProjectContextRepo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_focus_stamps_only_on_a_real_change() -> None:
    session = _mock_session(_result(None))

    with _patch_factory(session):
        await PgProjectContextRepo().update_focus("brain-v42", "a new focus")

    _assert_conditional_stamp(_statements(session)[0])


@pytest.mark.asyncio
async def test_partial_update_stamps_when_it_carries_a_focus() -> None:
    """`update()` is a generic partial write; it can still move the focus."""
    session = _mock_session(_result(None))

    with _patch_factory(session):
        await PgProjectContextRepo().update(
            uuid4(), ProjectContextUpdate(current_focus="moved by a partial update")
        )

    _assert_conditional_stamp(_statements(session)[0])


@pytest.mark.asyncio
async def test_partial_update_without_a_focus_leaves_the_stamp_alone() -> None:
    """Renaming a project is not writing a focus, so the age must not move."""
    session = _mock_session(_result(None))

    with _patch_factory(session):
        await PgProjectContextRepo().update(uuid4(), ProjectContextUpdate(name="Renamed"))

    assert "focus_updated_at" not in _set_clause(_statements(session)[0])


@pytest.mark.asyncio
async def test_get_or_create_stamps_the_conflict_branch_against_the_stored_row() -> None:
    """On conflict the upsert overwrites `current_focus`, so it must date it."""
    session = _mock_session(_result())

    with _patch_factory(session):
        await PgProjectContextRepo().get_or_create(
            ProjectContextCreate(project_key="brain-v42", name="B", description="d")
        )

    sql = _sql(_statements(session)[0])
    assert "ON CONFLICT" in sql, sql
    assert "excluded.current_focus" in sql, sql
    assert "project_contexts.current_focus IS DISTINCT FROM" in sql, sql
    assert "ELSE project_contexts.focus_updated_at" in sql, sql


@pytest.mark.asyncio
async def test_create_dates_a_focus_supplied_at_insert_time() -> None:
    session = _mock_session(_result())

    with _patch_factory(session):
        await PgProjectContextRepo().create(
            ProjectContextCreate(
                project_key="brain-v42",
                name="B",
                description="d",
                current_focus="born with a focus",
            )
        )

    assert "focus_updated_at" in _sql(_statements(session)[0])


@pytest.mark.asyncio
async def test_create_without_a_focus_leaves_the_stamp_null() -> None:
    """NULL reads as "no focus was ever written", which is true here."""
    session = _mock_session(_result())

    with _patch_factory(session):
        await PgProjectContextRepo().create(
            ProjectContextCreate(project_key="brain-v42", name="B", description="d")
        )

    compiled = _statements(session)[0].compile(dialect=postgresql.dialect(as_uuid=True))
    assert compiled.params.get("focus_updated_at") is None


# ---------------------------------------------------------------------------
# RoadmapService — the batch that consumes the CAS token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_focus_batch_stamps_only_on_a_real_change() -> None:
    """A blockers-only batch re-sends the same focus to spend the CAS token.

    That is a roadmap write, not a focus write, and it must not rejuvenate the
    focus age.
    """
    context_row = {"id": uuid4(), "current_focus": "unchanged", "focus_revision": 7}
    session = _mock_session(
        _result(context_row),
        _result({"current_focus": "unchanged", "focus_revision": 8}),
    )
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)

    await RoadmapService(MagicMock(return_value=context)).update_project_focus(
        "brain-v42",
        "unchanged",
        expected_focus_revision=7,
        blockers=["a blocker"],
    )

    _assert_conditional_stamp(_statements(session)[1])


# ---------------------------------------------------------------------------
# PgBrainSessionRepo — brain_session_end applying next_focus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_end_stamps_only_on_a_real_change() -> None:
    """`brain_session_end` rewrites the whole blob every time it closes.

    Re-posting the previous prose verbatim is the copy-forward this column
    exists to expose, so an identical blob must leave the age untouched.
    """
    session = _mock_session(_result({"current_focus": "carried", "focus_revision": 8}))

    await PgBrainSessionRepo()._apply_focus_if_current(
        session,
        {"current_focus": "carried", "focus_revision": 7},
        "brain-v42",
        "carried",
        7,
    )

    _assert_conditional_stamp(_statements(session)[0])


@pytest.mark.asyncio
async def test_session_end_does_not_touch_the_focus_on_a_conflict() -> None:
    session = _mock_session()

    _, outcome = await PgBrainSessionRepo()._apply_focus_if_current(
        session,
        {"current_focus": "carried", "focus_revision": 9},
        "brain-v42",
        "next",
        7,
    )

    assert outcome.value == "conflict"
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def test_focus_stamp_helper_is_the_single_definition_of_the_rule() -> None:
    """One expression, imported by every writer.

    Six call sites spread over three modules is exactly how a rule rots: the
    seventh writer copies five lines and quietly drops the ELSE branch.
    """
    from brain_v42.db.focus_stamp import focus_stamp

    stamp = focus_stamp(sa.literal("some focus"))
    sql = re.sub(r"\s+", " ", str(stamp.compile(dialect=postgresql.dialect())))

    assert sql.startswith("CASE WHEN")
    assert "project_contexts.current_focus IS DISTINCT FROM" in sql
    assert "THEN now()" in sql
    assert "ELSE project_contexts.focus_updated_at" in sql


def test_every_focus_writer_uses_the_shared_stamp() -> None:
    """A grep-level guard: no module may hand-roll the comparison."""
    from pathlib import Path

    root = Path(__file__).parents[3] / "src" / "brain_v42"
    writers = (
        root / "repositories" / "pg_project_context.py",
        root / "repositories" / "pg_brain_session.py",
        root / "services" / "roadmap_service.py",
    )
    for module in writers:
        source = module.read_text(encoding="utf-8")
        assert "focus_stamp" in source, module
        assert "is_distinct_from" not in source, module
