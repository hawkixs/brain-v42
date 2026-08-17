"""Integration tests: get_keys_by_group() returns colon-sub-partitions too.

Root issue: agents writing knowledge under colon-prefixed keys like
"red-shrik:agent" never register a project_contexts row, so group-scoped
search misses their entries. After fix, get_keys_by_group() must scan the
5 knowledge tables and include any colon-sub-partition whose base key
belongs to the group.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.learning import LearningCreate
from brain_v42.models.project_context import ProjectContextCreate
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_project_context import PgProjectContextRepo

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    # "integ-" prefix is required for conftest.py cleanup fixture.
    return f"integ-{uuid.uuid4().hex[:8]}"


def _unique_group() -> str:
    return f"integ-group-{uuid.uuid4().hex[:6]}"


class TestGetKeysByGroupColonSubPartitions:
    """Group scan must include colon-sub-partitions even when unregistered."""

    @pytest_asyncio.fixture
    async def project_context_repo(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> PgProjectContextRepo:
        monkeypatch.setattr(
            "brain_v42.repositories.pg_base.get_session_factory",
            lambda: session_factory,
        )
        return PgProjectContextRepo()

    @pytest_asyncio.fixture
    async def learning_repo(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> PgLearningRepo:
        return PgLearningRepo(session_factory=session_factory)

    async def test_base_key_returned_when_registered(
        self,
        project_context_repo: PgProjectContextRepo,
    ) -> None:
        """Regression: a plain base key registered with a group still returns."""
        group = _unique_group()
        base = _unique_key()
        await project_context_repo.get_or_create(
            ProjectContextCreate(
                project_key=base, name=base, description="test", project_group=group
            )
        )
        keys = await project_context_repo.get_keys_by_group(group)
        assert set(keys) == {base}

    async def test_colon_subpartition_returned_when_base_has_group(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """Core fix: an orphan colon-sub-partition is included via knowledge scan."""
        group = _unique_group()
        base = _unique_key()
        sub = f"{base}:agent"

        # base registered with the group
        await project_context_repo.get_or_create(
            ProjectContextCreate(
                project_key=base, name=base, description="test", project_group=group
            )
        )
        # sub-partition NEVER calls get_or_create — only writes a learning
        await learning_repo.create(
            LearningCreate(
                topic="orphan colon-partition learning",
                insight="scanned from learnings table",
                project_key=sub,
            )
        )

        keys = await project_context_repo.get_keys_by_group(group)
        assert set(keys) == {base, sub}

    async def test_colon_subpartition_isolated_per_group(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """Colon-sub-partitions don't leak across groups (negative + positive assertion)."""
        group_a = _unique_group()
        group_b = _unique_group()
        base_a = _unique_key()
        base_b = _unique_key()
        sub_b = f"{base_b}:agent"

        await project_context_repo.get_or_create(
            ProjectContextCreate(
                project_key=base_a, name=base_a, description="test", project_group=group_a
            )
        )
        await project_context_repo.get_or_create(
            ProjectContextCreate(
                project_key=base_b, name=base_b, description="test", project_group=group_b
            )
        )
        # Learning written under sub_b — base_b is in group_b, not group_a.
        await learning_repo.create(
            LearningCreate(
                topic="group-isolated sub",
                insight="belongs to group_b only",
                project_key=sub_b,
            )
        )

        # Negative: sub_b not in group_a results
        keys_a = await project_context_repo.get_keys_by_group(group_a)
        assert set(keys_a) == {base_a}
        assert sub_b not in keys_a

        # Positive: sub_b IS in group_b results
        keys_b = await project_context_repo.get_keys_by_group(group_b)
        assert set(keys_b) == {base_b, sub_b}

    async def test_empty_group_returns_empty(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """An unknown group with zero base keys returns [] — never leaks colon keys."""
        empty_group = _unique_group()

        # Write a learning under SOME colon key — it must NOT leak to empty_group.
        await learning_repo.create(
            LearningCreate(
                topic="orphan with no registered base",
                insight="must not leak",
                project_key=f"{_unique_key()}:agent",
            )
        )

        keys = await project_context_repo.get_keys_by_group(empty_group)
        assert keys == []

    async def test_colon_key_with_existing_project_contexts_row_dedups(
        self,
        project_context_repo: PgProjectContextRepo,
        learning_repo: PgLearningRepo,
    ) -> None:
        """A colon-sub-partition that IS registered in project_contexts appears exactly once."""
        group = _unique_group()
        base = _unique_key()
        sub = f"{base}:agent"

        # base registered
        await project_context_repo.get_or_create(
            ProjectContextCreate(
                project_key=base, name=base, description="test", project_group=group
            )
        )
        # sub ALSO registered in project_contexts (colon-key with group set)
        await project_context_repo.get_or_create(
            ProjectContextCreate(project_key=sub, name=sub, description="test", project_group=group)
        )
        # sub also writes knowledge
        await learning_repo.create(
            LearningCreate(
                topic="dedup test",
                insight="should appear once",
                project_key=sub,
            )
        )

        keys = await project_context_repo.get_keys_by_group(group)
        # Exactly 2 keys, no duplicates — UNION dedups across base + sub paths.
        assert sorted(keys) == sorted([base, sub])
        assert len(keys) == 2
