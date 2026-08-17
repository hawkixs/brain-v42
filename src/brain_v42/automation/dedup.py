"""Periodic feature deduplication owned by the automation bounded context."""

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

    async def merge_features(
        self,
        session: AsyncSession,
        target: FeatureCandidate,
        source: FeatureCandidate,
    ) -> bool: ...


class OwnershipGate(Protocol):
    """Admission gate checked around each dedup mutation."""

    def ensure_owned(self) -> None: ...


async def run_dedup_loop(
    dedup_job: FeatureDedupJobProtocol,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = 21600.0,
    ownership: OwnershipGate | None = None,
) -> None:
    """Run periodic feature deduplication."""
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

                consumed_ids: set[object] = set()
                for target, source, score in candidates:
                    if target.id in consumed_ids or source.id in consumed_ids:
                        logger.info(
                            "dedup_loop.skipped_consumed",
                            project_key=project_key,
                            target=str(target.name),
                            source=str(source.name),
                            score=score,
                        )
                        continue
                    try:
                        async with session_factory() as session:
                            if ownership is not None:
                                ownership.ensure_owned()
                            merged = await dedup_job.merge_features(session, target, source)
                            if ownership is not None:
                                ownership.ensure_owned()
                            await session.commit()
                            if ownership is not None:
                                ownership.ensure_owned()
                    except asyncio.CancelledError:
                        raise
                    except OwnershipLostError:
                        raise
                    except Exception as exc:
                        logger.exception(
                            "dedup_loop.candidate_error",
                            project_key=project_key,
                            target=str(target.name),
                            source=str(source.name),
                            score=score,
                            error_type=type(exc).__name__,
                            exc_info=True,
                        )
                        continue
                    if ownership is not None:
                        ownership.ensure_owned()
                    if merged:
                        consumed_ids.add(source.id)
                        logger.info(
                            "dedup_loop.merged",
                            project_key=project_key,
                            target=str(target.name),
                            source=str(source.name),
                            score=score,
                        )
                    else:
                        logger.info(
                            "dedup_loop.skipped_missing",
                            project_key=project_key,
                            target=str(target.name),
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
