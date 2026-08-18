"""The single construction path for the embedding client.

Every runtime builds its embedding service here, so an operator switching
``BRAIN_EMBEDDING_BACKEND`` switches all of them at once. A construction site
that bypasses this function pins one runtime to a different backend than the
other eight, which is the kind of split that only shows up as unexplained
search-quality drift.
"""

from __future__ import annotations

import structlog

from brain_v42.config import Settings
from brain_v42.services.embedding_wire import EmbeddingWire, OpenAIWire, ShimWire
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService

logger = structlog.get_logger(__name__)


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
