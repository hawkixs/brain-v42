"""Unit tests for RunbookService.create_with_promotion (T5 of Dream v3 plan).

Mirror of T4 for runbooks — no auto_accept. Verifies the service delegates
to PgRunbookRepo.create_with_promotion and that graph post-commit failures
don't propagate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.runbook import Runbook, RunbookCreate, RunbookStep
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.runbook_service import RunbookService


def _make_runbook(**kwargs) -> Runbook:
    now = datetime.now(UTC)
    defaults = {
        "id": uuid.uuid4(),
        "title": "rb",
        "description": "d",
        "project_key": "brain-v42",
        "trigger": "t",
        "prerequisites": [],
        "steps": [RunbookStep(order=1, title="s")],
        "rollback_steps": [],
        "estimated_duration": None,
        "tags": [],
        "execution_count": 0,
        "last_executed_at": None,
        "last_execution_status": None,
        "embedding": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(kwargs)
    return Runbook.model_validate(defaults)


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.create_with_promotion = AsyncMock()
    repo.set_embedding_if_current = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.2] * 1536)
    return svc


@pytest.mark.asyncio
async def test_create_with_promotion_delegates_to_repo_with_embedding(
    mock_repo: MagicMock, mock_embedding_svc: MagicMock
) -> None:
    """Promotion commits with null, then enriches the resulting Runbook."""
    runbook = _make_runbook(title="T")
    mock_repo.create_with_promotion.return_value = runbook
    mock_repo.set_embedding_if_current.return_value = runbook.model_copy(
        update={"embedding": [0.2] * 1536}
    ).model_dump()
    service = RunbookService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)

    source_id = uuid.uuid4()
    data = RunbookCreate(
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[RunbookStep(order=1, title="s")],
        rollback_steps=[],
        tags=[],
    )
    result = await service.create_with_promotion(
        data=data,
        source_learning_id=source_id,
        dream_run_id=7,
    )

    assert result.id == runbook.id
    assert result.embedding == [0.2] * 1536
    mock_embedding_svc.embed.assert_awaited_once()
    mock_repo.create_with_promotion.assert_awaited_once_with(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        dream_run_id=7,
    )
    mock_repo.set_embedding_if_current.assert_awaited_once_with(
        runbook.id,
        [0.2] * 1536,
        expected_updated_at=runbook.updated_at,
    )


@pytest.mark.asyncio
async def test_create_with_promotion_commits_before_embedding_and_survives_outage(
    mock_repo: MagicMock,
    mock_embedding_svc: MagicMock,
) -> None:
    events: list[str] = []
    runbook = _make_runbook()

    async def create_promoted(**kwargs):
        events.append("create_with_promotion")
        return runbook

    async def fail_embedding(text):
        events.append("embed")
        raise EmbeddingUnavailable("offline", kind="unreachable")

    mock_repo.create_with_promotion.side_effect = create_promoted
    mock_embedding_svc.embed.side_effect = fail_embedding
    service = RunbookService(pg_repo=mock_repo, embedding_svc=mock_embedding_svc)
    source_id = uuid.uuid4()
    data = RunbookCreate(
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[RunbookStep(order=1, title="s")],
    )

    result = await service.create_with_promotion(data, source_id, dream_run_id=7)

    assert result is runbook
    assert events == ["create_with_promotion", "embed"]
    mock_repo.create_with_promotion.assert_awaited_once_with(
        data=data,
        embedding=None,
        source_learning_id=source_id,
        dream_run_id=7,
    )
    mock_repo.set_embedding_if_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_with_promotion_no_embedding_svc_passes_none(
    mock_repo: MagicMock,
) -> None:
    """No embedding_svc → repo receives embedding=None."""
    mock_repo.create_with_promotion.return_value = _make_runbook()
    service = RunbookService(pg_repo=mock_repo, embedding_svc=None)

    await service.create_with_promotion(
        data=RunbookCreate(
            title="T",
            description="d",
            project_key="brain-v42",
            trigger="t",
            steps=[RunbookStep(order=1, title="s")],
            rollback_steps=[],
            tags=[],
        ),
        source_learning_id=uuid.uuid4(),
        dream_run_id=None,
    )
    assert mock_repo.create_with_promotion.await_args.kwargs["embedding"] is None


@pytest.mark.asyncio
async def test_create_with_promotion_graph_failure_does_not_break_call(
    mock_repo: MagicMock,
) -> None:
    """A raising graph mock must not undo the PG write."""
    runbook = _make_runbook()
    mock_repo.create_with_promotion.return_value = runbook

    broken_graph = MagicMock()
    broken_graph.upsert_node = AsyncMock(side_effect=RuntimeError("neo4j down"))
    broken_graph.link_to_project = AsyncMock()

    service = RunbookService(pg_repo=mock_repo, embedding_svc=None, graph=broken_graph)

    result = await service.create_with_promotion(
        data=RunbookCreate(
            title="T",
            description="d",
            project_key="brain-v42",
            trigger="t",
            steps=[RunbookStep(order=1, title="s")],
            rollback_steps=[],
            tags=[],
        ),
        source_learning_id=uuid.uuid4(),
        dream_run_id=None,
    )
    assert result is runbook
