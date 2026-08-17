"""Tests for post-commit embedding enrichment."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from brain_v42.services.embedding_enrichment import (
    EmbeddingEnrichmentService,
    EnrichmentStatus,
)
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable


class _EmbeddingService:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding

    async def embed(self, text: str) -> list[float]:
        return self.embedding


class _Repository:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.set_calls: list[dict] = []

    async def set_embedding_if_current(self, entity_id, embedding, **kwargs):
        self.set_calls.append(
            {
                "entity_id": entity_id,
                "embedding": embedding,
                **kwargs,
            }
        )
        return self.row

    async def get_by_id(self, entity_id):
        return self.row


class _NoUpdateRepository(_Repository):
    async def set_embedding_if_current(self, entity_id, embedding, **kwargs):
        self.set_calls.append(
            {
                "entity_id": entity_id,
                "embedding": embedding,
                **kwargs,
            }
        )
        return None


class _FailingRepository(_Repository):
    async def set_embedding_if_current(self, entity_id, embedding, **kwargs):
        raise RuntimeError("database unavailable")


class _UnavailableEmbeddingService:
    async def embed(self, text: str) -> list[float]:
        raise EmbeddingUnavailable("offline", kind="unreachable")


class _SlowEmbeddingService:
    async def embed(self, text: str) -> list[float]:
        await asyncio.sleep(1)
        return [0.1] * 1536


@pytest.mark.asyncio
async def test_enrich_stores_vector_for_unchanged_pending_row() -> None:
    entity_id = uuid.uuid4()
    updated_at = datetime.now(UTC)
    embedding = [0.1] * 1536
    repo = _Repository({"id": entity_id, "embedding": embedding})
    service = EmbeddingEnrichmentService(_EmbeddingService(embedding))

    result = await service.enrich(
        repo=repo,
        entity_type="decision",
        entity_id=entity_id,
        text="canonical embedding text",
        expected_updated_at=updated_at,
    )

    assert result.status is EnrichmentStatus.STORED
    assert result.embedding == embedding
    assert result.row == repo.row
    assert repo.set_calls == [
        {
            "entity_id": entity_id,
            "embedding": embedding,
            "expected_updated_at": updated_at,
        }
    ]


@pytest.mark.asyncio
async def test_enrich_preserves_pending_row_when_embedding_unavailable() -> None:
    repo = _Repository(None)
    service = EmbeddingEnrichmentService(_UnavailableEmbeddingService())

    result = await service.enrich(
        repo=repo,
        entity_type="learning",
        entity_id=uuid.uuid4(),
        text="canonical embedding text",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status is EnrichmentStatus.UNAVAILABLE
    assert result.embedding is None
    assert repo.set_calls == []


@pytest.mark.asyncio
async def test_enrich_preserves_pending_row_when_repository_write_fails() -> None:
    repo = _FailingRepository(None)
    service = EmbeddingEnrichmentService(_EmbeddingService([0.1] * 1536))

    result = await service.enrich(
        repo=repo,
        entity_type="decision",
        entity_id=uuid.uuid4(),
        text="canonical embedding text",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status.value == "failed"
    assert result.embedding is None


@pytest.mark.asyncio
async def test_enrich_bounds_request_path_latency() -> None:
    repo = _Repository(None)
    service = EmbeddingEnrichmentService(
        _SlowEmbeddingService(),
        timeout_seconds=0.01,
    )

    result = await service.enrich(
        repo=repo,
        entity_type="snippet",
        entity_id=uuid.uuid4(),
        text="canonical embedding text",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status is EnrichmentStatus.UNAVAILABLE
    assert repo.set_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_row", "expected_status"),
    [
        ({"id": uuid.uuid4(), "embedding": None}, "stale"),
        (None, "missing"),
    ],
    ids=["stale", "missing"],
)
async def test_enrich_classifies_compare_and_set_miss(current_row, expected_status) -> None:
    entity_id = uuid.uuid4()
    repo = _NoUpdateRepository(current_row)
    service = EmbeddingEnrichmentService(_EmbeddingService([0.1] * 1536))

    result = await service.enrich(
        repo=repo,
        entity_type="adr",
        entity_id=entity_id,
        text="canonical embedding text",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status.value == expected_status
    assert result.embedding is None


@pytest.mark.asyncio
async def test_enrich_rejects_invalid_vector_shape_before_repository_write() -> None:
    repo = _Repository({"id": uuid.uuid4()})
    service = EmbeddingEnrichmentService(_EmbeddingService([0.1]))

    result = await service.enrich(
        repo=repo,
        entity_type="runbook",
        entity_id=uuid.uuid4(),
        text="canonical embedding text",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status.value == "failed"
    assert result.embedding is None
    assert repo.set_calls == []


@pytest.mark.asyncio
async def test_enrich_rejects_non_list_embedding_response() -> None:
    repo = _Repository({"id": uuid.uuid4()})
    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=None)
    service = EmbeddingEnrichmentService(embedding_svc)

    result = await service.enrich(
        repo=repo,
        entity_type="decision",
        entity_id=uuid.uuid4(),
        text="safe content",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status.value == "failed"
    assert repo.set_calls == []


@pytest.mark.asyncio
async def test_enrich_contains_repository_failure_while_classifying_cas_miss() -> None:
    repo = _NoUpdateRepository({"id": uuid.uuid4()})
    repo.get_by_id = AsyncMock(side_effect=RuntimeError("db unavailable"))  # type: ignore[method-assign]
    service = EmbeddingEnrichmentService(_EmbeddingService([0.1] * 1536))

    result = await service.enrich(
        repo=repo,
        entity_type="decision",
        entity_id=uuid.uuid4(),
        text="safe content",
        expected_updated_at=datetime.now(UTC),
    )

    assert result.status.value == "failed"
