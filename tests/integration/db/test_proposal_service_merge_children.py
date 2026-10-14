"""Prove curation preserves one-hop merge pointers against the migrated schema."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import features, project_contexts, roadmap_curation_proposals
from brain_v42.services.proposal_service import ProposalService


@pytest.mark.asyncio
async def test_curation_merge_repoints_children_and_persists_audit(
    db_session: AsyncSession,
) -> None:
    """Keep all writes inside an outer rollback while exercising the public transaction."""
    project_key = f"test-curation-{uuid4().hex[:12]}"
    loser, into = uuid4(), uuid4()
    child_ids = sorted([uuid4(), uuid4()], key=str)
    await db_session.execute(
        project_contexts.insert().values(
            project_key=project_key, name="Curation merge", description="Merge invariant test"
        )
    )
    for feature_id in [loser, into, *child_ids]:
        await db_session.execute(
            features.insert().values(
                id=feature_id,
                project_key=project_key,
                name="Merge feature",
                description="Merge invariant test",
                status="archived" if feature_id in child_ids else "building",
                merged_into=loser if feature_id in child_ids else None,
            )
        )
    proposal_id = (
        await db_session.execute(
            roadmap_curation_proposals.insert()
            .values(
                feature_id=loser,
                op="merge",
                payload={"into": str(into)},
                created_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            .returning(roadmap_curation_proposals.c.id)
        )
    ).scalar_one()
    factory = async_sessionmaker(
        bind=await db_session.connection(),
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    result = await ProposalService(factory, AsyncMock(), AsyncMock()).apply_roadmap_curation(
        proposal_id
    )

    pointers = dict(
        (
            await db_session.execute(
                sa.select(features.c.id, features.c.merged_into).where(
                    features.c.id.in_([loser, *child_ids])
                )
            )
        ).all()
    )
    assert pointers == dict.fromkeys([loser, *child_ids], into)
    source = features.alias("source")
    target = features.alias("target")
    chains = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(source.join(target, source.c.merged_into == target.c.id))
        .where(source.c.project_key == project_key, target.c.merged_into.is_not(None))
    )
    assert chains == 0
    persisted = (
        await db_session.execute(
            sa.select(
                roadmap_curation_proposals.c.status, roadmap_curation_proposals.c.apply_log
            ).where(roadmap_curation_proposals.c.id == proposal_id)
        )
    ).one()
    assert persisted.status == "applied"
    assert persisted.apply_log == result.apply_log
    assert persisted.apply_log["repointed_children"] == [str(child_id) for child_id in child_ids]
