"""Two-project PostgreSQL proof for point-of-use CRUD authorization."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.decision import DecisionCreate, DecisionUpdate
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_decision_crud_rechecks_project_at_each_sql_operation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PgDecisionRepo(session_factory)
    owned_project = f"integ-sec1b-owned-{uuid.uuid4().hex[:10]}"
    foreign_project = f"integ-sec1b-foreign-{uuid.uuid4().hex[:10]}"
    owned = None
    foreign = None
    foreign_reference = None
    foreign_target = None
    same_project_reference = None
    same_project_target = None

    try:
        owned = await repo.create(
            DecisionCreate(
                title="SEC1b owned",
                description="owned row",
                reasoning="project isolation",
                project_key=owned_project,
            )
        )
        foreign = await repo.create(
            DecisionCreate(
                title="SEC1b foreign",
                description="foreign row",
                reasoning="project isolation",
                project_key=foreign_project,
            )
        )

        assert await repo.get_by_id(owned.id, project_key=owned_project) is not None
        assert await repo.get_by_id(foreign.id, project_key=owned_project) is None

        assert (
            await repo.update(
                foreign.id,
                DecisionUpdate(title="must not change"),
                project_key=owned_project,
            )
            is None
        )
        foreign_after_update = await repo.get_by_id(foreign.id)
        assert foreign_after_update is not None
        assert foreign_after_update.title == "SEC1b foreign"

        assert await repo.delete(foreign.id, project_key=owned_project) is False
        assert await repo.get_by_id(foreign.id) is not None

        foreign_reference = await repo.create(
            DecisionCreate(
                title="SEC1b foreign reference",
                description="foreign reference row",
                reasoning="cross-project supersession guard",
                project_key=foreign_project,
            )
        )
        foreign_target = await repo.supersede(
            foreign_reference.id,
            DecisionCreate(
                title="SEC1b owned target",
                description="owned target row",
                reasoning="cross-project supersession guard",
                project_key=owned_project,
            ),
        )

        assert await repo.delete(foreign_target.id, project_key=owned_project) is False
        assert await repo.get_by_id(foreign_target.id) is not None
        foreign_reference_after = await repo.get_by_id(foreign_reference.id)
        assert foreign_reference_after is not None
        assert foreign_reference_after.superseded_by == foreign_target.id

        same_project_reference = await repo.create(
            DecisionCreate(
                title="SEC1b same-project reference",
                description="same-project reference row",
                reasoning="same-project supersession cleanup",
                project_key=owned_project,
            )
        )
        same_project_target = await repo.supersede(
            same_project_reference.id,
            DecisionCreate(
                title="SEC1b same-project target",
                description="same-project target row",
                reasoning="same-project supersession cleanup",
                project_key=owned_project,
            ),
        )

        assert await repo.delete(same_project_target.id, project_key=owned_project) is True
        assert await repo.get_by_id(same_project_target.id) is None
        same_project_reference_after = await repo.get_by_id(same_project_reference.id)
        assert same_project_reference_after is not None
        assert same_project_reference_after.superseded_by is None
        assert same_project_reference_after.status == "active"

        updated = await repo.update(
            owned.id,
            DecisionUpdate(title="SEC1b owned updated"),
            project_key=owned_project,
        )
        assert updated is not None
        assert updated.title == "SEC1b owned updated"
        assert await repo.delete(owned.id, project_key=owned_project) is True
        assert await repo.get_by_id(owned.id) is None
    finally:
        if foreign_reference is not None:
            await repo.delete(foreign_reference.id)
        if same_project_reference is not None:
            await repo.delete(same_project_reference.id)
        if foreign_target is not None:
            await repo.delete(foreign_target.id)
        if same_project_target is not None:
            await repo.delete(same_project_target.id)
        if owned is not None:
            await repo.delete(owned.id)
        if foreign is not None:
            await repo.delete(foreign.id)


@pytest.mark.asyncio
async def test_scoped_plan_delete_preserves_foreign_project_chunk(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owned_project = f"integ-sec1b-plan-owned-{uuid.uuid4().hex[:8]}"
    foreign_project = f"integ-sec1b-plan-foreign-{uuid.uuid4().hex[:8]}"
    plan_id = None

    async with session_factory() as session:
        repo = PgIndexedPlanRepo(session)
        try:
            plan_id = await repo.upsert_plan_with_chunks(
                IndexedPlanCreate(
                    file_path=f"/tmp/sec1b-plan-{uuid.uuid4().hex}.md",
                    title="SEC1b scoped plan",
                    plan_type="plan",
                    project_key=owned_project,
                    content_hash=uuid.uuid4().hex * 2,
                    content="# SEC1b scoped plan",
                    chunk_count=1,
                    word_count=4,
                ),
                [0.1] * 1536,
                [
                    IndexedPlanChunkCreate(
                        section_title="Scope",
                        section_path="Scope",
                        content="## Scope\n\nScoped chunk",
                        section_order=0,
                        word_count=3,
                        project_key=owned_project,
                        plan_type="plan",
                    )
                ],
                [[0.1] * 1536],
            )
            await session.execute(
                sa.text(
                    "UPDATE indexed_plan_chunks "
                    "SET project_key = :foreign_project WHERE plan_id = :plan_id"
                ),
                {"foreign_project": foreign_project, "plan_id": plan_id},
            )
            await session.commit()

            assert await repo.delete(plan_id, project_key=owned_project) is False
            persisted = await repo.get_with_chunks(plan_id)
            assert persisted is not None
            assert persisted[0].project_key == owned_project
            assert len(persisted[1]) == 1
            assert persisted[1][0].project_key == foreign_project
        finally:
            if plan_id is not None:
                await session.rollback()
                await repo.delete(plan_id)
