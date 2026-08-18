"""The single construction path for the embedding client.

Every runtime builds its embedding service here, so an operator switching
``BRAIN_EMBEDDING_BACKEND`` switches all of them at once. A construction site
that bypasses this function pins one runtime to a different backend than the
other eight, which is the kind of split that only shows up as unexplained
search-quality drift.
"""

from __future__ import annotations

import structlog
from pydantic import ValidationError

from brain_v42.config import Settings, get_settings
from brain_v42.services.embedding_wire import EmbeddingWire, OpenAIWire, ShimWire
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.rerank_wire import CohereRerankWire, RerankWire, ShimRerankWire
from brain_v42.services.reranker_client import RerankerClient

logger = structlog.get_logger(__name__)


def settings_for_standalone_script(postgres_url: str) -> Settings:
    """Settings for a script that already knows its own database URL.

    ``get_settings()`` requires POSTGRES_URL in the environment and insists on
    the ``postgresql+asyncpg://`` form. Standalone scripts here take
    ``--postgres-url``, default to a plain ``postgresql://`` DSN and are
    expected to run from any working directory with no ``.env`` in reach — so
    calling ``get_settings()`` unguarded turns "no env var" into a crash before
    the script does anything at all.

    Everything except the database URL still comes from the environment, so the
    embedding backend, model and prefixes are the ones actually configured.
    """
    try:
        return get_settings()
    except ValidationError:
        return Settings(postgres_url=as_asyncpg_dsn(postgres_url))


def as_asyncpg_dsn(url: str) -> str:
    """Normalise a plain ``postgresql://`` DSN to the driver form Settings wants."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def build_embedding_wire(settings: Settings) -> EmbeddingWire:
    """Select the wire format named by ``settings.embedding_backend``."""
    if settings.embedding_backend == "openai":
        return OpenAIWire(model=settings.embedding_model)
    return ShimWire()


def build_embedding_service(settings: Settings) -> GPUEmbeddingService:
    """Build the embedding client configured for this deployment.

    Defaults reproduce the shim contract exactly: same routes, same bodies, no
    prefixes, no Authorization header.
    """
    wire = build_embedding_wire(settings)
    logger.info(
        "embedding_factory.built",
        backend=settings.embedding_backend,
        base_url=settings.embedding_service_url,
        model=settings.embedding_model if settings.embedding_backend == "openai" else None,
        query_prefixed=bool(settings.embedding_query_prefix),
        document_prefixed=bool(settings.embedding_document_prefix),
    )
    return GPUEmbeddingService(
        base_url=settings.embedding_service_url,
        timeout=settings.embedding_timeout,
        wire=wire,
        query_prefix=settings.embedding_query_prefix,
        document_prefix=settings.embedding_document_prefix,
        api_key=settings.embedding_api_key.get_secret_value(),
    )


def build_rerank_wire(settings: Settings) -> RerankWire:
    """Select the rerank wire format named by ``settings.rerank_backend``."""
    if settings.rerank_backend == "cohere":
        return CohereRerankWire(model=settings.rerank_model)
    return ShimRerankWire()


def build_reranker_client(settings: Settings) -> RerankerClient:
    """Build the reranker client configured for this deployment.

    Reranking stays best-effort: an unavailable or misconfigured reranker
    makes HybridReranker fall back to RRF ordering rather than fail a search.
    """
    return RerankerClient(
        base_url=settings.reranker_url,
        timeout=settings.reranker_timeout,
        wire=build_rerank_wire(settings),
        api_key=settings.rerank_api_key.get_secret_value(),
    )
