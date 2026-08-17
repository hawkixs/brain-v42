"""Post-commit embedding enrichment for durable PG-first writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import structlog

from brain_v42.db.tables import _EMBEDDING_DIM

logger = structlog.get_logger(__name__)


class EnrichmentStatus(StrEnum):
    """Outcome of one post-commit enrichment attempt."""

    STORED = "stored"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    MISSING = "missing"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmbeddingEnrichmentResult:
    """Bounded result returned to creation services."""

    status: EnrichmentStatus
    embedding: list[float] | None = None
    row: dict[str, Any] | None = None


class EmbeddingService(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class EmbeddingRepository(Protocol):
    async def set_embedding_if_current(
        self,
        entity_id: UUID,
        embedding: list[float],
        *,
        expected_updated_at: datetime,
    ) -> dict[str, Any] | None: ...

    async def get_by_id(self, entity_id: UUID) -> Any | None: ...


class EmbeddingEnrichmentService:
    """Store a derived vector after the authoritative row has committed."""

    def __init__(self, embedding_svc: EmbeddingService, *, timeout_seconds: float = 5.0) -> None:
        self._embedding_svc = embedding_svc
        self._timeout_seconds = timeout_seconds

    async def enrich(
        self,
        *,
        repo: EmbeddingRepository,
        entity_type: str,
        entity_id: UUID,
        text: str,
        expected_updated_at: datetime,
    ) -> EmbeddingEnrichmentResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                embedding = await self._embedding_svc.embed(text)
        except Exception as exc:  # noqa: BLE001 - derived work must not undo the PG commit
            logger.warning(
                "embedding_enrichment.unavailable",
                entity_type=entity_type,
                entity_id=str(entity_id),
                reason=type(exc).__name__,
            )
            return EmbeddingEnrichmentResult(status=EnrichmentStatus.UNAVAILABLE)
        if not isinstance(embedding, list) or len(embedding) != _EMBEDDING_DIM:
            logger.error(
                "embedding_enrichment.invalid_dimension",
                entity_type=entity_type,
                entity_id=str(entity_id),
                observed_dimension=len(embedding) if isinstance(embedding, list) else None,
                expected_dimension=_EMBEDDING_DIM,
            )
            return EmbeddingEnrichmentResult(status=EnrichmentStatus.FAILED)
        try:
            stored_row = await repo.set_embedding_if_current(
                entity_id,
                embedding,
                expected_updated_at=expected_updated_at,
            )
        except Exception as exc:  # noqa: BLE001 - the authoritative row is already durable
            logger.error(
                "embedding_enrichment.store_failed",
                entity_type=entity_type,
                entity_id=str(entity_id),
                reason=type(exc).__name__,
            )
            return EmbeddingEnrichmentResult(status=EnrichmentStatus.FAILED)
        if stored_row is None:
            try:
                current_row = await repo.get_by_id(entity_id)
            except Exception as exc:  # noqa: BLE001 - classification is derived work too
                logger.error(
                    "embedding_enrichment.classify_failed",
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    reason=type(exc).__name__,
                )
                return EmbeddingEnrichmentResult(status=EnrichmentStatus.FAILED)
            status = EnrichmentStatus.MISSING if current_row is None else EnrichmentStatus.STALE
            return EmbeddingEnrichmentResult(status=status)
        return EmbeddingEnrichmentResult(
            status=EnrichmentStatus.STORED,
            embedding=embedding,
            row=stored_row,
        )
