"""Real PostgreSQL proof for the ticket extraction corpus dedup gate."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from scripts.ticket_extract import (
    CorpusDedupUnavailable,
    ProposalDraft,
    deduplicate_drafts,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import _EMBEDDING_DIM, decisions, learnings, projects

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _draft(project_key: str) -> ProposalDraft:
    return ProposalDraft(
        ticket_id=uuid4(),
        target_type="learning",
        target_project=project_key,
        payload={
            "topic": "Canonical API contract",
            "insight": "Responses use camelCase by design.",
            "tags": ["api"],
        },
        rationale="durable contract",
    )


async def test_dedup_is_cross_type_project_scoped_and_ignores_inactive_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    project_with_match = f"integ-extract-dedup-{uuid4().hex[:8]}"
    other_project = f"integ-extract-other-{uuid4().hex[:8]}"
    decision_id = uuid4()
    archived_learning_id = uuid4()
    null_learning_id = uuid4()
    zero_learning_id = uuid4()
    vector = [0.0] * _EMBEDDING_DIM
    vector[0] = 1.0
    zero_vector = [0.0] * _EMBEDDING_DIM

    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    projects.insert(),
                    [
                        {"project_key": project_with_match},
                        {"project_key": other_project},
                    ],
                )
                await session.execute(
                    decisions.insert().values(
                        id=decision_id,
                        title="Canonical API contract",
                        description="Responses use camelCase.",
                        reasoning="Shared serializer contract.",
                        project_key=project_with_match,
                        embedding=vector,
                    )
                )
                await session.execute(
                    learnings.insert().values(
                        id=zero_learning_id,
                        topic="Non-comparable zero vector",
                        insight="This row must make the corpus gate fail closed.",
                        project_key=project_with_match,
                        embedding=zero_vector,
                    )
                )

        embedding = AsyncMock()
        embedding.embed.return_value = vector
        matching_draft = _draft(project_with_match)
        scoped_draft = _draft(other_project)

        with pytest.raises(CorpusDedupUnavailable, match="embedding backlog"):
            await deduplicate_drafts(
                session_factory,
                embedding,
                [matching_draft],
            )

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    learnings.update()
                    .where(learnings.c.id == zero_learning_id)
                    .values(freshness_status="archived")
                )

        result = await deduplicate_drafts(
            session_factory,
            embedding,
            [matching_draft, scoped_draft],
        )

        assert result.kept == [scoped_draft]
        assert len(result.duplicates) == 1
        assert result.duplicates[0].draft is matching_draft
        assert result.duplicates[0].entity_type == "decision"
        assert result.duplicates[0].entity_id == decision_id
        assert result.duplicates[0].similarity == pytest.approx(1.0)

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    decisions.update()
                    .where(decisions.c.id == decision_id)
                    .values(status="superseded")
                )
                await session.execute(
                    learnings.insert().values(
                        id=archived_learning_id,
                        topic="Canonical API contract",
                        insight="Responses use camelCase by design.",
                        project_key=project_with_match,
                        freshness_status="archived",
                        embedding=vector,
                    )
                )

        inactive_result = await deduplicate_drafts(
            session_factory,
            embedding,
            [matching_draft],
        )
        assert inactive_result.kept == [matching_draft]
        assert inactive_result.duplicates == []

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    learnings.insert().values(
                        id=null_learning_id,
                        topic="Unembedded live knowledge",
                        insight="This row must make the corpus gate fail closed.",
                        project_key=project_with_match,
                    )
                )

        with pytest.raises(CorpusDedupUnavailable, match="embedding backlog"):
            await deduplicate_drafts(
                session_factory,
                embedding,
                [matching_draft],
            )
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.delete(learnings).where(
                        learnings.c.id.in_(
                            [archived_learning_id, null_learning_id, zero_learning_id]
                        )
                    )
                )
                await session.execute(sa.delete(decisions).where(decisions.c.id == decision_id))
                # Migration 033 keeps canonical brain_entities after source
                # deletion. The integration suite's session cleanup removes
                # those ledger rows before deleting the two integ-* projects.
