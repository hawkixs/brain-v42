"""Integration test for PgADRRepo.vector_search against real pgvector.

Regression guard for a latent bug: pg_adr.vector_search SELECTed the cosine
distance expression (``embedding <=> query``) WITHOUT a Float cast. Because
``op("<=>")`` carries no return_type, SQLAlchemy inferred the result column's
type as ``Vector`` (the left operand's type) and applied pgvector's Vector
result-processor to the scalar distance float at fetch time, executing
``value[1:-1].split(",")`` on a float -> ``TypeError: 'float' object is not
subscriptable``.

The bug only fired when the ``adrs`` table actually had rows with non-NULL
embeddings (otherwise ``embedding IS NOT NULL`` filtered everything and the
processor was never invoked) — which is exactly why it stayed latent until
ADRs were populated. Mocked unit tests cannot catch it: it requires a real
pgvector round-trip.

This must run against a real PostgreSQL+pgvector instance (see
tests/integration/conftest.py). PgADRRepo accepts session_factory injection.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import _EMBEDDING_DIM
from brain_v42.models.adr import ADRCreate
from brain_v42.repositories.pg_adr import PgADRRepo

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    # Kebab-case (not integ_<...>) to satisfy the project_key validator; the
    # session cleanup uses LIKE 'integ_%' where '_' is a single-char wildcard,
    # so 'integ-...' is still cleaned up.
    return f"integ-adr-{uuid.uuid4().hex[:8]}"


def _unit_vec(dim: int = _EMBEDDING_DIM) -> list[float]:
    v = [0.0] * dim
    v[0] = 1.0
    return v


async def test_vector_search_returns_results_for_embedded_adr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """vector_search returns (ADR, distance) tuples for an embedded ADR.

    Pre-fix this raised TypeError: 'float' object is not subscriptable while
    deserializing the SELECTed distance column.
    """
    repo = PgADRRepo(session_factory)
    project_key = _unique_key()
    vec = _unit_vec()

    await repo.create(
        ADRCreate(
            title="Vector search regression ADR",
            context="Some context",
            decision="Some decision",
            consequences="Some consequences",
            project_key=project_key,
        ),
        embedding=vec,
    )

    results = await repo.vector_search(query_embedding=vec, project_key=project_key, limit=5)

    assert len(results) >= 1
    adr, distance = results[0]
    assert adr.project_key == project_key
    assert isinstance(distance, float)
