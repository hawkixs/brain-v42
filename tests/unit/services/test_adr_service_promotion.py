"""Unit tests for ADRService.create_with_promotion (T4 of Dream v3 plan).

Verifies the service is a thin orchestration layer over
PgADRRepo.create_with_promotion: embedding is computed once, repo receives
the right kwargs, and graph post-commit side-effects never propagate
exceptions back to the caller (graph_helpers swallow internally).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.adr import ADR, ADRCreate
from brain_v42.services.adr_service import ADRService
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable


def _make_adr(**kwargs) -> ADR:
    defaults = {
        "id": uuid.uuid4(),
        "number": 1,
        "title": "t",
        "context": "c",
        "decision": "d",
        "consequences": "q",
        "alternatives_considered": [],
        "project_key": "brain-v42",
        "tags": [],
        "status": "accepted",
        "decided_at": None,
        "superseded_by": None,
        "embedding": None,
        "metadata": {},
    }
    defaults.update(kwargs)
    return ADR.model_validate(defaults)


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.create_with_promotion = AsyncMock()
    repo.set_embedding_if_current = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1] * 1536)
    return svc


@pytest.mark.asyncio
async def test_create_with_promotion_delegates_to_repo_with_embedding(
    mock_repo: MagicMock, mock_embedding_svc: MagicMock
) -> None:
    """Promotion commits with null, then enriches the resulting ADR."""
    adr = _make_adr(status="accepted")
    mock_repo.create_with_promotion.return_value = adr
    mock_repo.set_embedding_if_current.return_value = adr.model_copy(
        update={"embedding": [0.1] * 1536}
    ).model_dump()
    service = ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)

    source_id = uuid.uuid4()
    data = ADRCreate(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
        alternatives_considered=[],
        tags=[],
    )
    result = await service.create_with_promotion(
        data=data,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=42,
    )

    assert result.id == adr.id
    assert result.embedding == [0.1] * 1536
    mock_embedding_svc.embed.assert_awaited_once()
    mock_repo.create_with_promotion.assert_awaited_once_with(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=42,
    )
    mock_repo.set_embedding_if_current.assert_awaited_once_with(
        adr.id,
        [0.1] * 1536,
        expected_updated_at=adr.updated_at,
    )


@pytest.mark.asyncio
async def test_create_with_promotion_commits_before_embedding_and_survives_outage(
    mock_repo: MagicMock,
    mock_embedding_svc: MagicMock,
) -> None:
    events: list[str] = []
    adr = _make_adr()

    async def create_promoted(**kwargs):
        events.append("create_with_promotion")
        return adr

    async def fail_embedding(text):
        events.append("embed")
        raise EmbeddingUnavailable("offline", kind="unreachable")

    mock_repo.create_with_promotion.side_effect = create_promoted
    mock_embedding_svc.embed.side_effect = fail_embedding
    service = ADRService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)
    source_id = uuid.uuid4()
    data = ADRCreate(
        title="T",
        context="c",
        decision="d",
        consequences="q",
        project_key="brain-v42",
    )

    result = await service.create_with_promotion(
        data,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=42,
    )

    assert result is adr
    assert events == ["create_with_promotion", "embed"]
    mock_repo.create_with_promotion.assert_awaited_once_with(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        auto_accept=True,
        dream_run_id=42,
    )
    mock_repo.set_embedding_if_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_with_promotion_no_embedding_svc_passes_none(
    mock_repo: MagicMock,
) -> None:
    """No embedding_svc → repo receives embedding=None."""
    adr = _make_adr()
    mock_repo.create_with_promotion.return_value = adr
    service = ADRService(pg_repo=mock_repo, embedding_svc=None)

    source_id = uuid.uuid4()
    await service.create_with_promotion(
        data=ADRCreate(
            title="T",
            context="c",
            decision="d",
            consequences="q",
            project_key="brain-v42",
            alternatives_considered=[],
            tags=[],
        ),
        source_learning_id=source_id,
        auto_accept=False,
        dream_run_id=None,
    )
    kwargs = mock_repo.create_with_promotion.await_args.kwargs
    assert kwargs["embedding"] is None
    assert kwargs["auto_accept"] is False


@pytest.mark.asyncio
async def test_create_with_promotion_graph_failure_does_not_break_call(
    mock_repo: MagicMock,
) -> None:
    """A raising graph mock must not undo the PG write — service returns the ADR."""
    adr = _make_adr()
    mock_repo.create_with_promotion.return_value = adr

    broken_graph = MagicMock()
    broken_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))
    broken_graph.link_to_project = AsyncMock()

    service = ADRService(pg_repo=mock_repo, embedding_svc=None, graph=broken_graph)

    result = await service.create_with_promotion(
        data=ADRCreate(
            title="T",
            context="c",
            decision="d",
            consequences="q",
            project_key="brain-v42",
            alternatives_considered=[],
            tags=[],
        ),
        source_learning_id=uuid.uuid4(),
        auto_accept=True,
        dream_run_id=None,
    )
    assert result is adr
