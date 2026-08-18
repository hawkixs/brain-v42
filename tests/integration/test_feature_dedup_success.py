"""Real PostgreSQL proof for the complete feature-dedup success path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    feature_artifacts,
    features,
    gitlab_events,
    project_contexts,
    roadmap_curation_proposals,
)
from brain_v42.services.feature_dedup_job import FeatureDedupJob

pytestmark = pytest.mark.integration


class _Embedding:
    async def embed(self, _text: str) -> list[float]:
        return [0.7] * 1536

    async def embed_query(self, _text: str) -> list[float]:
        return [0.7] * 1536


async def test_feature_dedup_success_preserves_history_and_reparents_dependents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_key = f"integ-dedup-success-{uuid4().hex[:8]}"
    target_id = uuid4()
    source_id = uuid4()
    descendant_id = uuid4()
    artifact_id = uuid4()
    event_id = f"integ-dedup-{uuid4()}"

    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Dedup success integration",
                description="Complete transactional merge proof",
            )
        )
        await session.execute(
            features.insert(),
            [
                {
                    "id": target_id,
                    "project_key": project_key,
                    "name": "Dedup survivor",
                    "description": "survivor description",
                    "embedding": [0.1] * 1536,
                    "status": "planned",
                    "merged_into": None,
                },
                {
                    "id": source_id,
                    "project_key": project_key,
                    "name": "Dedup source",
                    "description": "source description",
                    "embedding": [0.2] * 1536,
                    "status": "planned",
                    "merged_into": None,
                },
                {
                    "id": descendant_id,
                    "project_key": project_key,
                    "name": "Prior descendant",
                    "description": "must be flattened to the survivor",
                    "embedding": None,
                    "status": "archived",
                    "merged_into": source_id,
                },
            ],
        )
        await session.execute(
            feature_artifacts.insert().values(
                feature_id=source_id,
                artifact_type="learning",
                artifact_id=artifact_id,
                similarity_score=0.91,
            )
        )
        await session.execute(
            gitlab_events.insert().values(
                gitlab_event_id=event_id,
                event_type="merge_request",
                project_key=project_key,
                ref="feat/dedup-success",
                title="Dedup success integration",
                feature_id=source_id,
            )
        )
        proposal_id = (
            await session.execute(
                roadmap_curation_proposals.insert()
                .values(
                    op="archive",
                    feature_id=source_id,
                    payload={},
                    rationale="history must survive dedup",
                )
                .returning(roadmap_curation_proposals.c.id)
            )
        ).scalar_one()

    job = FeatureDedupJob(
        session_factory,
        MagicMock(name="reranker"),
        _Embedding(),  # type: ignore[arg-type]
    )
    async with session_factory.begin() as session:
        merged = await job.merge_features(
            session,
            SimpleNamespace(id=target_id, name="Dedup survivor"),
            SimpleNamespace(id=source_id, name="Dedup source"),
        )

    assert merged is True
    async with session_factory() as session:
        feature_rows = {
            row.id: row
            for row in (
                await session.execute(
                    sa.select(
                        features.c.id,
                        features.c.status,
                        features.c.merged_into,
                        features.c.description,
                        features.c.embedding,
                    ).where(features.c.id.in_([target_id, source_id, descendant_id]))
                )
            ).all()
        }
        artifact_owner = await session.scalar(
            sa.select(feature_artifacts.c.feature_id).where(
                feature_artifacts.c.artifact_id == artifact_id
            )
        )
        event_owner = await session.scalar(
            sa.select(gitlab_events.c.feature_id).where(gitlab_events.c.gitlab_event_id == event_id)
        )
        preserved_proposal = await session.scalar(
            sa.select(roadmap_curation_proposals.c.feature_id).where(
                roadmap_curation_proposals.c.id == proposal_id
            )
        )

    assert feature_rows[target_id].status != "archived"
    assert feature_rows[target_id].merged_into is None
    assert feature_rows[target_id].description == ("survivor description\n---\nsource description")
    assert tuple(float(value) for value in feature_rows[target_id].embedding) == pytest.approx(
        (0.7,) * 1536
    )
    assert feature_rows[source_id].status == "archived"
    assert feature_rows[source_id].merged_into == target_id
    assert feature_rows[descendant_id].merged_into == target_id
    assert artifact_owner == target_id
    assert event_owner == target_id
    assert preserved_proposal == source_id
