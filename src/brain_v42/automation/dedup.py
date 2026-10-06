"""Periodic feature deduplication owned by the automation bounded context.

The loop SIGNALS probable duplicates, it never merges them. Operator ruling
9e21964f (extension of d4648d84): nothing ever merges on a reranker score, and
the score that selects a pair here is a reranker score, under every backend.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.db.tables import project_contexts

logger = structlog.get_logger(__name__)


class FeatureCandidate(Protocol):
    id: object
    name: object


class FeatureDedupJobProtocol(Protocol):
    """Typed operations consumed by the scheduler."""

    async def find_candidates(
        self,
        project_key: str,
    ) -> list[tuple[FeatureCandidate, FeatureCandidate, float]]: ...


class OwnershipGate(Protocol):
    """Admission gate checked around each dedup pass."""

    def ensure_owned(self) -> None: ...


async def run_dedup_loop(
    dedup_job: FeatureDedupJobProtocol,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = 21600.0,
    ownership: OwnershipGate | None = None,
) -> None:
    """Run periodic duplicate detection; record probable duplicates, never merge.

    WHY no merge: the pair is selected by a reranker score, and an operator
    ruling (9e21964f, extending d4648d84) forbids any merge on such a score,
    shim backend included. A human decides from the logged signal. The loop
    therefore opens no write session and holds no merge bookkeeping: the only
    session it opens is the read of the project keys.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            if ownership is not None:
                ownership.ensure_owned()

            async with session_factory() as session:
                result = await session.execute(sa.select(project_contexts.c.project_key))
                project_keys = [row[0] for row in result.fetchall()]

            for project_key in project_keys:
                try:
                    candidates = await dedup_job.find_candidates(project_key)
                    if ownership is not None:
                        ownership.ensure_owned()
                except asyncio.CancelledError:
                    raise
                except OwnershipLostError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "dedup_loop.project_error",
                        project_key=project_key,
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                    continue

                for target, source, score in candidates:
                    logger.info(
                        "dedup_loop.probable_duplicate",
                        project_key=project_key,
                        target_id=str(target.id),
                        target=str(target.name),
                        source_id=str(source.id),
                        source=str(source.name),
                        score=score,
                    )
        except asyncio.CancelledError:
            raise
        except OwnershipLostError:
            raise
        except Exception as exc:
            logger.exception(
                "dedup_loop.error",
                error_type=type(exc).__name__,
                exc_info=True,
            )
