"""Real-PostgreSQL safety proofs for competing roadmap proposal applies."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import feature_artifacts, features, roadmap_curation_proposals
from brain_v42.services.proposal_service import (
    ProposalMutationResult,
    ProposalService,
    ProposalStateConflictError,
)

pytestmark = pytest.mark.integration


async def _seed_competing_merges(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reverse: bool,
) -> tuple[list[UUID], list[int], UUID]:
    project_key = f"integ-proposal-race-{uuid4().hex[:8]}"
    artifact_id = uuid4()
    async with session_factory.begin() as session:
        feature_rows = (
            (
                await session.execute(
                    features.insert()
                    .values(
                        [
                            {
                                "project_key": project_key,
                                "name": name,
                                "description": "proposal concurrency proof",
                            }
                            for name in ("A", "B", "C")
                        ]
                    )
                    .returning(features.c.id)
                )
            )
            .scalars()
            .all()
        )
        source, first_target, second_target = feature_rows
        await session.execute(
            feature_artifacts.insert().values(
                feature_id=source,
                artifact_type="learning",
                artifact_id=artifact_id,
                similarity_score=0.9,
            )
        )
        merge_pairs = (
            [(source, first_target), (first_target, source)]
            if reverse
            else [(source, first_target), (source, second_target)]
        )
        proposal_ids = (
            (
                await session.execute(
                    roadmap_curation_proposals.insert()
                    .values(
                        [
                            {
                                "op": "merge",
                                "feature_id": loser,
                                "payload": {"into": str(target)},
                                "rationale": "concurrency proof",
                            }
                            for loser, target in merge_pairs
                        ]
                    )
                    .returning(roadmap_curation_proposals.c.id)
                )
            )
            .scalars()
            .all()
        )
    return list(feature_rows), list(proposal_ids), artifact_id


async def _apply_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
    proposal_ids: list[int],
) -> list[ProposalMutationResult | BaseException]:
    service = ProposalService(session_factory, AsyncMock(), AsyncMock())
    return list(
        await asyncio.wait_for(
            asyncio.gather(
                *(service.apply_roadmap_curation(item) for item in proposal_ids),
                return_exceptions=True,
            ),
            timeout=10,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [True, False])
async def test_competing_merges_apply_once_without_cycle_or_artifact_loss(
    session_factory: async_sessionmaker[AsyncSession],
    reverse: bool,
) -> None:
    feature_ids, proposal_ids, artifact_id = await _seed_competing_merges(
        session_factory,
        reverse=reverse,
    )

    outcomes = await _apply_concurrently(session_factory, proposal_ids)

    assert sum(isinstance(item, ProposalMutationResult) for item in outcomes) == 1
    assert sum(isinstance(item, ProposalStateConflictError) for item in outcomes) == 1

    async with session_factory() as session:
        proposal_states = (
            await session.execute(
                sa.select(
                    roadmap_curation_proposals.c.id,
                    roadmap_curation_proposals.c.status,
                ).where(roadmap_curation_proposals.c.id.in_(proposal_ids))
            )
        ).all()
        feature_states = (
            await session.execute(
                sa.select(features.c.id, features.c.status, features.c.merged_into).where(
                    features.c.id.in_(feature_ids)
                )
            )
        ).all()
        artifact_owner = (
            await session.execute(
                sa.select(feature_artifacts.c.feature_id).where(
                    feature_artifacts.c.artifact_type == "learning",
                    feature_artifacts.c.artifact_id == artifact_id,
                )
            )
        ).scalar_one()

    assert sorted(status for _proposal_id, status in proposal_states) == ["applied", "proposed"]
    by_id = {
        feature_id: (status, merged_into) for feature_id, status, merged_into in feature_states
    }
    archived = [feature_id for feature_id, state in by_id.items() if state[0] == "archived"]
    assert len(archived) == 1
    loser = archived[0]
    winner = by_id[loser][1]
    assert winner in by_id
    assert by_id[winner][1] is None
    assert artifact_owner == winner
