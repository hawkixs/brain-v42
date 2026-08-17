"""Transaction-level behavior for explicit roadmap feature creation."""

from __future__ import annotations

import importlib.util
from importlib import import_module
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.models.feature import FeatureCreate
from brain_v42.services.feature_creation_service import (
    FeatureAlreadyExistsError,
    FeatureCreationService,
    FeatureEmbeddingError,
    FeatureProjectNotFoundError,
)

_METADATA = sa.MetaData()
_PROJECT_CONTEXTS = sa.Table(
    "project_contexts",
    _METADATA,
    sa.Column("project_key", sa.String(50), primary_key=True),
)
_FEATURES = sa.Table(
    "features",
    _METADATA,
    sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
    sa.Column("project_key", sa.String(50), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("embedding", sa.JSON),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("pinned", sa.Boolean, nullable=False),
    sa.Column("merged_into", sa.Uuid(as_uuid=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


class _EmbeddingService:
    def __init__(self, result: object | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return cast(list[float], self.result)


@pytest.fixture
async def feature_store() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(_METADATA.create_all)
        await connection.execute(_PROJECT_CONTEXTS.insert().values(project_key="brain-v42"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_feature_creation_service_module_exists() -> None:
    assert importlib.util.find_spec("brain_v42.services.feature_creation_service") is not None


def test_feature_creation_service_exposes_create_use_case() -> None:
    module = import_module("brain_v42.services.feature_creation_service")

    assert hasattr(module, "FeatureCreationService")
    assert hasattr(module.FeatureCreationService, "create")


def test_feature_creation_failures_have_domain_errors() -> None:
    module = import_module("brain_v42.services.feature_creation_service")

    assert issubclass(module.FeatureProjectNotFoundError, module.FeatureCreationError)
    assert issubclass(module.FeatureAlreadyExistsError, module.FeatureCreationError)
    assert issubclass(module.FeatureEmbeddingError, module.FeatureCreationError)


@pytest.mark.asyncio
async def test_create_persists_one_visible_feature_with_its_embedding(
    feature_store: async_sessionmaker[AsyncSession],
) -> None:
    embedding = [0.25] * 1536
    embedding_service = _EmbeddingService(embedding)
    service = FeatureCreationService(
        feature_store,
        embedding_service,
        features_table=_FEATURES,
        project_contexts_table=_PROJECT_CONTEXTS,
    )
    payload = FeatureCreate(
        project_key="brain-v42",
        name="Explicit roadmap feature",
        description="Deliver a fail-closed creation tool.",
        status="design",
        pinned=False,
    )

    created = await service.create(payload)

    assert created.project_key == "brain-v42"
    assert created.name == payload.name
    assert created.description == payload.description
    assert created.status == "design"
    assert created.pinned is False
    assert embedding_service.calls == [
        "Explicit roadmap feature\n\nDeliver a fail-closed creation tool."
    ]
    async with feature_store() as session:
        rows = (await session.execute(sa.select(_FEATURES))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["embedding"] == embedding


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_project_before_embedding_or_insert(
    feature_store: async_sessionmaker[AsyncSession],
) -> None:
    embedding_service = _EmbeddingService([0.25] * 1536)
    service = FeatureCreationService(
        feature_store,
        embedding_service,
        features_table=_FEATURES,
        project_contexts_table=_PROJECT_CONTEXTS,
    )
    payload = FeatureCreate(
        project_key="missing-project",
        name="Orphan feature",
        description="Must never be persisted.",
    )

    with pytest.raises(FeatureProjectNotFoundError, match="missing-project"):
        await service.create(payload)

    assert embedding_service.calls == []
    async with feature_store() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(_FEATURES))
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "embedding_result",
    [
        RuntimeError("gpu unavailable"),
        None,
        [0.25] * 12,
        ["0.25"] * 1536,
        [True] * 1536,
        [float("nan")] * 1536,
        [float("inf")] * 1536,
    ],
    ids=[
        "provider-error",
        "none",
        "wrong-dimension",
        "non-numeric",
        "boolean",
        "nan",
        "infinity",
    ],
)
async def test_create_fails_closed_when_embedding_is_unusable(
    feature_store: async_sessionmaker[AsyncSession],
    embedding_result: object | Exception,
) -> None:
    service = FeatureCreationService(
        feature_store,
        _EmbeddingService(embedding_result),
        features_table=_FEATURES,
        project_contexts_table=_PROJECT_CONTEXTS,
    )
    payload = FeatureCreate(
        project_key="brain-v42",
        name="Embedding required",
        description="A feature without its vector must not be created.",
    )

    with pytest.raises(FeatureEmbeddingError, match="embedding"):
        await service.create(payload)

    async with feature_store() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(_FEATURES))
    assert count == 0


@pytest.mark.asyncio
async def test_create_uses_the_configured_embedding_dimension(
    feature_store: async_sessionmaker[AsyncSession],
) -> None:
    embedding = [0.25, 0.5, 0.75]
    service = FeatureCreationService(
        feature_store,
        _EmbeddingService(embedding),
        embedding_dimension=3,
        features_table=_FEATURES,
        project_contexts_table=_PROJECT_CONTEXTS,
    )

    created = await service.create(
        FeatureCreate(
            project_key="brain-v42",
            name="Configured vector dimension",
            description="The service follows runtime configuration.",
        )
    )

    assert created.name == "Configured vector dimension"
    async with feature_store() as session:
        stored = (await session.execute(sa.select(_FEATURES.c.embedding))).scalar_one()
    assert stored == embedding


@pytest.mark.asyncio
async def test_create_rejects_an_exact_normalized_duplicate_before_embedding(
    feature_store: async_sessionmaker[AsyncSession],
) -> None:
    embedding_service = _EmbeddingService([0.25] * 1536)
    service = FeatureCreationService(
        feature_store,
        embedding_service,
        features_table=_FEATURES,
        project_contexts_table=_PROJECT_CONTEXTS,
    )
    await service.create(
        FeatureCreate(
            project_key="brain-v42",
            name="Explicit Roadmap Feature",
            description="First canonical definition.",
        )
    )

    with pytest.raises(FeatureAlreadyExistsError, match="already exists"):
        await service.create(
            FeatureCreate(
                project_key="brain-v42",
                name="  explicit roadmap feature  ",
                description="A second definition must be rejected.",
            )
        )

    assert len(embedding_service.calls) == 1
    async with feature_store() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(_FEATURES))
    assert count == 1
