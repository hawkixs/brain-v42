"""Fail-closed use case for explicit roadmap feature creation."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import features as _default_features
from brain_v42.db.tables import project_contexts as _default_project_contexts
from brain_v42.models.feature import Feature, FeatureCreate

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class FeatureCreationError(RuntimeError):
    """Base class for safe, user-actionable creation failures."""


class FeatureProjectNotFoundError(FeatureCreationError, LookupError):
    """The requested project context does not exist."""


class FeatureAlreadyExistsError(FeatureCreationError):
    """An exact normalized feature name already exists in the project."""


class FeatureEmbeddingError(FeatureCreationError):
    """A durable feature embedding could not be produced."""


class FeatureCreationService:
    """Create explicit roadmap features with a locked transactional insert."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_svc: EmbeddingProvider,
        *,
        embedding_dimension: int = 1536,
        features_table: Table = _default_features,
        project_contexts_table: Table = _default_project_contexts,
    ) -> None:
        if embedding_dimension < 1:
            raise ValueError("embedding_dimension must be positive")
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._embedding_dimension = embedding_dimension
        self._features = features_table
        self._project_contexts = project_contexts_table

    async def create(self, payload: FeatureCreate) -> Feature:
        project_query = sa.select(self._project_contexts.c.project_key).where(
            self._project_contexts.c.project_key == payload.project_key
        )
        normalized_name = payload.name.lower()
        duplicate_query = (
            sa.select(self._features.c.id, self._features.c.name)
            .where(
                self._features.c.project_key == payload.project_key,
                sa.func.lower(sa.func.trim(self._features.c.name)) == normalized_name,
            )
            .limit(1)
        )
        async with self._sf() as session:
            project_key = (await session.execute(project_query)).scalar_one_or_none()
            duplicate = (await session.execute(duplicate_query)).mappings().one_or_none()
        if project_key is None:
            raise FeatureProjectNotFoundError(
                f"project {payload.project_key!r} does not exist; no feature created"
            )
        self._raise_if_duplicate(duplicate, payload)

        embedding_text = f"{payload.name}\n\n{payload.description}"
        try:
            embedding = await self._embedding_svc.embed(embedding_text)
        except Exception as exc:
            logger.warning(
                "feature_creation.embedding_failed",
                project_key=payload.project_key,
                exc_info=True,
            )
            raise FeatureEmbeddingError(
                "feature embedding unavailable; no feature created"
            ) from exc
        embedding = self._validate_embedding(embedding)
        now = datetime.now(UTC)
        statement = (
            sa.insert(self._features)
            .values(
                id=uuid4(),
                project_key=payload.project_key,
                name=payload.name,
                description=payload.description,
                embedding=embedding,
                status=payload.status,
                status_updated_at=now,
                pinned=payload.pinned,
                created_at=now,
                updated_at=now,
            )
            .returning(self._features)
        )
        async with self._sf.begin() as session:
            locked_project = (
                await session.execute(project_query.with_for_update())
            ).scalar_one_or_none()
            if locked_project is None:
                raise FeatureProjectNotFoundError(
                    f"project {payload.project_key!r} does not exist; no feature created"
                )
            duplicate = (await session.execute(duplicate_query)).mappings().one_or_none()
            self._raise_if_duplicate(duplicate, payload)
            row = (await session.execute(statement)).mappings().one()
        return Feature.model_validate(dict(row))

    def _validate_embedding(self, embedding: object) -> list[float]:
        if not isinstance(embedding, list) or len(embedding) != self._embedding_dimension:
            raise FeatureEmbeddingError(
                "feature embedding has an invalid dimension; no feature created"
            )

        validated: list[float] = []
        for component in embedding:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise FeatureEmbeddingError(
                    "feature embedding contains a non-numeric value; no feature created"
                )
            numeric_component = float(component)
            if not isfinite(numeric_component):
                raise FeatureEmbeddingError(
                    "feature embedding contains a non-finite value; no feature created"
                )
            validated.append(numeric_component)
        return validated

    @staticmethod
    def _raise_if_duplicate(
        duplicate: RowMapping | None,
        payload: FeatureCreate,
    ) -> None:
        if duplicate is None:
            return
        raise FeatureAlreadyExistsError(
            f"feature {duplicate['name']!r} [{str(duplicate['id'])[:8]}] already exists "
            f"in project {payload.project_key!r}; no feature created"
        )
