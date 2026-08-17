from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_entities, entity_relations, projects
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

pytestmark = pytest.mark.integration


async def _entity(
    session: AsyncSession,
    *,
    project_key: str | None,
    entity_type: str = "learning",
    lifecycle: str = "active",
    created_at: datetime,
) -> tuple[UUID, UUID | None]:
    entity_id = uuid4()
    source_uuid = None if entity_type == "domain" else uuid4()
    await session.execute(
        sa.insert(brain_entities).values(
            id=entity_id,
            entity_type=entity_type,
            entity_key=str(source_uuid or f"domain-{entity_id}"),
            source_uuid=source_uuid,
            project_key=project_key,
            scope_kind="project" if project_key else "global",
            lifecycle=lifecycle,
            created_at=created_at,
        )
    )
    return entity_id, source_uuid


async def test_lists_only_active_canonical_orphans_before_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:10]
    project_key = f"integ-orphans-{suffix}"
    other_project = f"integ-orphans-other-{suffix}"
    now = datetime.now(UTC)

    async with session_factory.begin() as session:
        await session.execute(
            sa.insert(projects),
            [{"project_key": project_key}, {"project_key": other_project}],
        )
        _, archived_source = await _entity(
            session,
            project_key=project_key,
            lifecycle="archived",
            created_at=now - timedelta(minutes=10),
        )
        _, expected_source = await _entity(
            session,
            project_key=project_key,
            created_at=now,
        )
        _, other_source = await _entity(
            session,
            project_key=other_project,
            created_at=now - timedelta(minutes=9),
        )
        related_source_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=8)
        )
        related_target_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=7)
        )
        await session.execute(
            sa.insert(entity_relations).values(
                source_entity_id=related_source_id,
                target_entity_id=related_target_id,
                relation_type="RELATED_TO",
                origin="integration",
                lifecycle="active",
            )
        )
        domain_member_id, _ = await _entity(
            session, project_key=project_key, created_at=now - timedelta(minutes=6)
        )
        domain_id, _ = await _entity(
            session,
            project_key=project_key,
            entity_type="domain",
            created_at=now - timedelta(minutes=5),
        )
        await session.execute(
            sa.insert(entity_relations).values(
                source_entity_id=domain_member_id,
                target_entity_id=domain_id,
                relation_type="BELONGS_TO_DOMAIN",
                origin="integration",
                lifecycle="active",
            )
        )

    repo = PgGraphLedgerRepo(session_factory)
    limited = await repo.list_active_classification_orphans(
        limit=1,
        project_key=project_key,
    )
    scoped = await repo.list_active_classification_orphans(
        limit=20,
        project_key=project_key,
    )
    other_scoped = await repo.list_active_classification_orphans(
        limit=20,
        project_key=other_project,
    )

    expected = [{"source_uuid": expected_source, "entity_type": "learning"}]
    assert limited == expected
    assert scoped == expected
    assert archived_source not in {row["source_uuid"] for row in scoped}
    assert other_scoped == [{"source_uuid": other_source, "entity_type": "learning"}]
