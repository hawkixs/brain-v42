"""FeatureDedupJob — periodic detection of probable duplicate features.

Scans all features in a project, finds near-duplicate pairs using a two-stage
pipeline (pgvector cosine similarity pre-filter, then cross-encoder reranker),
and returns them for SIGNALLING only.  It never merges: operator ruling 9e21964f
(extension of d4648d84) forbids any merge on a reranker score, under every
backend, and this job writes nothing.

Usage:
    job = FeatureDedupJob(session_factory, reranker)
    candidates = await job.find_candidates("brain_v42")
    for target, source, score in candidates:
        logger.info("probable duplicate", target=target.name, source=source.name)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import features
from brain_v42.services.rerank_calibration import RerankCalibration

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.reranker_client import RerankerClient

logger = structlog.get_logger(__name__)

# ── thresholds ──────────────────────────────────────────────────────────

COSINE_PREFILTER = 0.50
_TOP_K_NEIGHBORS = 3


class FeatureDedupJob:
    """Probable-duplicate detection using cosine pre-filter + cross-encoder reranker.

    Pipeline:
    1. Get all features for a project with embeddings
    2. For each feature, find top-3 neighbors via cosine similarity (>= 0.50)
    3. Run cross-encoder on pre-filtered pairs
    4. Score >= calibrated threshold -> probable duplicate, for signalling only
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        reranker: RerankerClient,
    ) -> None:
        self._sf = session_factory
        self._reranker = reranker

    async def find_candidates(
        self,
        project_key: str,
    ) -> list[tuple[Any, Any, float]]:
        """Find probable duplicate pairs using cosine pre-filter + cross-encoder.

        Read-only: the pairs are for signalling, never for merging.

        Returns:
            List of (target, source, score) tuples where target is the oldest
            feature of the pair and source the newest. Score is the reranker score.
        """
        cal = getattr(self._reranker, "calibration", None)
        if not isinstance(cal, RerankCalibration) or cal.dedup_signal is None:
            identity = getattr(cal, "identity", None)
            logger.warning(
                "feature_dedup.reranker_uncalibrated",
                identity=identity if isinstance(identity, str) else None,
            )
            return []

        async with self._sf() as session:
            # Step 1: get all features with embeddings
            all_features = await self._get_all_features(session, project_key)

            if len(all_features) < 2:
                return []

            # Step 2: for each feature, find top-K neighbors via cosine
            seen_pairs: set[tuple[str, str]] = set()
            pre_filtered: list[tuple[Any, Any, float]] = []

            for feature in all_features:
                neighbors = await self._find_neighbors(session, feature, project_key)
                for neighbor in neighbors:
                    pair_key: tuple[str, str] = (
                        min(str(feature.id), str(neighbor.id)),
                        max(str(feature.id), str(neighbor.id)),
                    )
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    # Determine target (oldest) and source (newest)
                    if feature.created_at <= neighbor.created_at:
                        target, source = feature, neighbor
                    else:
                        target, source = neighbor, feature

                    # `pinned` marks an explicit operator commitment, and the
                    # source is the one that DISAPPEARS. Absorbing a pinned
                    # feature therefore destroys a commitment — that happened on
                    # 2026-08-14 at 19:17 on a hand-created feature, with a
                    # reranker_score of 0.83 obtained by comparing NAMES only,
                    # never the descriptions that carry the scope.
                    #
                    # We refuse the pair instead of swapping the roles: having
                    # the pinned one absorb would decide the survivor on
                    # pinning rather than on age, and would still merge two
                    # scopes nothing proves identical. A pinned feature as
                    # TARGET stays allowed, that is the nominal case.
                    #
                    # `bool()` and not `is True`: the column is nullable, and
                    # NULL means "not pinned", not "unknown".
                    if bool(source.pinned):
                        logger.info(
                            "feature_dedup.pinned_source_skipped",
                            target_id=str(target.id),
                            source_id=str(source.id),
                        )
                        continue

                    cosine_score = float(neighbor.similarity)
                    pre_filtered.append((target, source, cosine_score))

            if not pre_filtered:
                return []

            # Step 3: run cross-encoder on pre-filtered pairs
            candidates: list[tuple[Any, Any, float]] = []
            for target, source, _cosine_score in pre_filtered:
                scores = await self._reranker.rerank(target.name, [source.name])
                reranker_score = scores[0] if scores else 0.0

                # Step 4: score >= threshold -> probable duplicate
                if reranker_score >= cal.dedup_signal:
                    candidates.append((target, source, reranker_score))
                    logger.info(
                        "feature_dedup.candidate_found",
                        target_id=str(target.id),
                        source_id=str(source.id),
                        reranker_score=reranker_score,
                    )

            return candidates

    # ── internal helpers ────────────────────────────────────────────────

    async def _get_all_features(
        self,
        session: AsyncSession,
        project_key: str,
    ) -> list[Any]:
        """Get live merge-root features for a project that have embeddings."""
        stmt = (
            sa.select(features)
            .where(
                features.c.project_key == project_key,
                features.c.embedding.isnot(None),
                features.c.status != "archived",
                features.c.merged_into.is_(None),
            )
            .order_by(features.c.created_at.asc())
        )
        result = await session.execute(stmt)
        return list(result.fetchall())

    async def _find_neighbors(
        self,
        session: AsyncSession,
        feature: Any,
        project_key: str,
    ) -> list[Any]:
        """Find top-K nearest neighbors for a feature via cosine similarity.

        Only returns neighbors with cosine similarity >= COSINE_PREFILTER.
        """
        feature_id = feature.id
        feature_embedding = feature.embedding

        similarity = (1 - features.c.embedding.cosine_distance(feature_embedding)).label(
            "similarity"
        )

        stmt = (
            sa.select(features, similarity)
            .where(
                features.c.project_key == project_key,
                features.c.embedding.isnot(None),
                features.c.status != "archived",
                features.c.merged_into.is_(None),
                features.c.id != feature_id,
                (1 - features.c.embedding.cosine_distance(feature_embedding)) >= COSINE_PREFILTER,
            )
            .order_by(similarity.desc())
            .limit(_TOP_K_NEIGHBORS)
        )
        result = await session.execute(stmt)
        return list(result.fetchall())
