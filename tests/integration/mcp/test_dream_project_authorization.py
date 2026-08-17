"""Real PostgreSQL ownership proofs for scoped Dream authorization."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    adrs,
    decisions,
    indexed_plans,
    learnings,
    project_contexts,
    runbooks,
    snippets,
)
from brain_v42.mcp.dream_project_authorization import (
    DreamObjectReference,
    PostgresDreamProjectResolver,
)

pytestmark = pytest.mark.integration


def _typed(entity_id: UUID, entity_type: str) -> DreamObjectReference:
    return DreamObjectReference(entity_id=entity_id, entity_type=entity_type)  # type: ignore[arg-type]


def _generic(entity_id: UUID) -> DreamObjectReference:
    return DreamObjectReference(entity_id=entity_id)


@pytest.mark.asyncio
async def test_postgres_resolver_is_project_authoritative_and_non_enumerating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_a = f"sec1b-a-{suffix}"
    project_b = f"sec1b-b-{suffix}"
    project_a_id, project_b_id = uuid4(), uuid4()
    owned_ids = {
        "decision": uuid4(),
        "learning": uuid4(),
        "snippet": uuid4(),
        "runbook": uuid4(),
        "adr": uuid4(),
        "plan": uuid4(),
    }
    foreign_id = uuid4()
    foreign_plan_id = uuid4()
    null_project_id = uuid4()
    ambiguous_id = uuid4()
    missing_id = uuid4()
    cleanup_ids: dict[sa.Table, set[UUID]] = {
        decisions: {owned_ids["decision"], foreign_id, ambiguous_id},
        learnings: {owned_ids["learning"]},
        snippets: {owned_ids["snippet"], null_project_id},
        runbooks: {owned_ids["runbook"]},
        adrs: {owned_ids["adr"]},
        indexed_plans: {owned_ids["plan"], foreign_plan_id},
    }

    try:
        async with session_factory() as session:
            await session.execute(
                sa.insert(project_contexts),
                [
                    {
                        "id": project_a_id,
                        "project_key": project_a,
                        "name": "SEC1b isolated project A",
                        "description": "integration authorization fixture",
                    },
                    {
                        "id": project_b_id,
                        "project_key": project_b,
                        "name": "SEC1b isolated project B",
                        "description": "integration authorization fixture",
                    },
                ],
            )
            await session.execute(
                sa.insert(decisions),
                [
                    {
                        "id": owned_ids["decision"],
                        "title": "SEC1b owned decision",
                        "description": "owned",
                        "reasoning": "integration proof",
                        "project_key": project_a,
                    },
                    {
                        "id": foreign_id,
                        "title": "SEC1b foreign decision",
                        "description": "foreign",
                        "reasoning": "integration proof",
                        "project_key": project_b,
                    },
                    {
                        "id": ambiguous_id,
                        "title": "SEC1b ambiguous decision",
                        "description": "same UUID in another table",
                        "reasoning": "integration proof",
                        "project_key": project_a,
                    },
                ],
            )
            await session.execute(
                sa.insert(learnings),
                [
                    {
                        "id": owned_ids["learning"],
                        "topic": "SEC1b owned learning",
                        "insight": "owned",
                        "project_key": project_a,
                    },
                ],
            )
            await session.execute(
                sa.insert(snippets),
                [
                    {
                        "id": owned_ids["snippet"],
                        "title": "SEC1b owned snippet",
                        "intention": "ownership proof",
                        "code": "pass",
                        "language": "python",
                        "project_key": project_a,
                    },
                    {
                        "id": null_project_id,
                        "title": "SEC1b null-project snippet",
                        "intention": "fail closed",
                        "code": "pass",
                        "language": "python",
                        "project_key": None,
                    },
                ],
            )
            await session.execute(
                sa.insert(runbooks).values(
                    id=owned_ids["runbook"],
                    title=f"SEC1b owned runbook {suffix}",
                    description="owned",
                    project_key=project_a,
                    trigger="integration test",
                )
            )
            await session.execute(
                sa.insert(adrs).values(
                    id=owned_ids["adr"],
                    number=1,
                    title="SEC1b owned ADR",
                    context="integration",
                    decision="authorize",
                    consequences="isolated",
                    project_key=project_a,
                )
            )
            await session.execute(
                sa.insert(indexed_plans),
                [
                    {
                        "id": owned_ids["plan"],
                        "file_path": f"/sec1b/{suffix}/owned.md",
                        "title": "SEC1b owned plan",
                        "plan_type": "plan",
                        "project_key": project_a,
                        "content_hash": uuid4().hex * 2,
                        "content": "owned",
                    },
                    {
                        "id": foreign_plan_id,
                        "file_path": f"/sec1b/{suffix}/foreign.md",
                        "title": "SEC1b foreign plan",
                        "plan_type": "plan",
                        "project_key": project_b,
                        "content_hash": uuid4().hex * 2,
                        "content": "foreign",
                    },
                ],
            )
            await session.commit()

        # Migration 033's write-through registry (brain_entities primary key)
        # forbids one UUID living in two knowledge tables, so cross-table
        # ambiguity can no longer be staged here; the resolver's ambiguity
        # rejection stays covered by unit tests on a trigger-free schema.
        with pytest.raises(IntegrityError):
            async with session_factory() as session:
                await session.execute(
                    sa.insert(learnings).values(
                        id=ambiguous_id,
                        topic="SEC1b ambiguous learning",
                        insight="same UUID in another table",
                        project_key=project_a,
                    )
                )
                await session.commit()

        resolver = PostgresDreamProjectResolver(session_factory)
        for entity_type, entity_id in owned_ids.items():
            assert await resolver.references_belong_to_project(
                project_a,
                [_typed(entity_id, entity_type)],
            )
        assert await resolver.references_belong_to_project(
            project_a,
            [
                _generic(owned_ids[entity_type])
                for entity_type in owned_ids
                if entity_type != "plan"
            ],
        )
        assert not await resolver.references_belong_to_project(
            project_a, [_typed(foreign_id, "decision")]
        )
        assert not await resolver.references_belong_to_project(
            project_a, [_typed(foreign_plan_id, "plan")]
        )
        assert not await resolver.references_belong_to_project(
            project_a, [_typed(null_project_id, "snippet")]
        )
        assert not await resolver.references_belong_to_project(project_a, [_generic(missing_id)])
        assert not await resolver.references_belong_to_project(
            project_a, [_generic(owned_ids["plan"])]
        )
    finally:
        async with session_factory() as session:
            for table, ids in cleanup_ids.items():
                await session.execute(sa.delete(table).where(table.c.id.in_(ids)))
            await session.execute(
                sa.delete(project_contexts).where(
                    project_contexts.c.id.in_((project_a_id, project_b_id))
                )
            )
            await session.commit()
