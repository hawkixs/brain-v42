"""Real PostgreSQL proof for project-scoped decay aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import learnings
from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.mcp.tools.decay_tools import register_decay_tools

pytestmark = pytest.mark.integration


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class UnusedResolver:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("decay aggregates must scope directly in PostgreSQL")


def _scope(project_key: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=project_key,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name="brain_decay_status",
    )


@pytest.mark.asyncio
async def test_scoped_decay_status_counts_only_owned_rows_and_cleans_up(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = uuid4().hex
    owned_project = f"integ-sec1b-decay-{token[:8]}"
    foreign_project = f"integ-sec1b-foreign-{token[:8]}"
    old = datetime.now(UTC) - timedelta(days=181)
    recent = datetime.now(UTC)
    inserted_ids: list[UUID] = []

    try:
        rows = [
            ("owned fresh", owned_project, "fresh", recent),
            ("owned stale", owned_project, "stale", recent),
            ("owned archived old", owned_project, "archived", old),
            ("owned archived recent", owned_project, "archived", recent),
            ("foreign fresh", foreign_project, "fresh", recent),
            ("foreign archived old", foreign_project, "archived", old),
            ("null archived old", None, "archived", old),
        ]
        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    learnings.insert()
                    .values(
                        [
                            {
                                "topic": f"SEC1b decay {token} {name}",
                                "insight": "Project-scoped aggregate proof",
                                "project_key": project_key,
                                "freshness_status": freshness,
                                "access_count": 0,
                                "updated_at": updated_at,
                            }
                            for name, project_key, freshness, updated_at in rows
                        ]
                    )
                    .returning(learnings.c.id)
                )
                inserted_ids.extend(result.scalars().all())

        mcp = MockMCP()
        register_decay_tools(mcp, session_factory)
        with bind_dream_project_scope(_scope(owned_project)):
            rendered = await mcp.registered["brain_decay_status"]()

        assert "learning: 1f 1s 2a 1d" in rendered
        assert "learning: 2f" not in rendered
        assert "3a" not in rendered
        assert "2d" not in rendered
    finally:
        async with session_factory() as session:
            async with session.begin():
                if inserted_ids:
                    await session.execute(
                        learnings.delete().where(learnings.c.id.in_(inserted_ids))
                    )
        async with session_factory() as session:
            remaining = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(learnings)
                    .where(learnings.c.id.in_(inserted_ids))
                )
            ).scalar_one()
        assert remaining == 0
