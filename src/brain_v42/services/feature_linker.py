"""FeatureLinker — auto-links artifacts to features by embedding similarity.

Called after each artifact insert in the service layer. Queries the features
table for matches (same project_key, cosine similarity >= threshold), then
inserts links into feature_artifacts.

When a ClusterGuard is provided **and** the caller passes a title, the linker
delegates to ClusterGuard.resolve() (which may link, merge, or create a
feature) and then inserts the feature_artifact link.  Otherwise the original
raw-SQL cosine-similarity path is used for backward compatibility.

Fire-and-forget: failures are logged but never block the artifact insert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import feature_artifacts, features

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from brain_v42.services.cluster_guard import ClusterGuard

logger = structlog.get_logger(__name__)

_DEFAULT_THRESHOLD = 0.70


class FeatureLinker:
    """Auto-links artifacts to features by cosine similarity."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        threshold: float = _DEFAULT_THRESHOLD,
        cluster_guard: ClusterGuard | None = None,
    ) -> None:
        self._sf = session_factory
        self._threshold = threshold
        self._cluster_guard = cluster_guard

    async def link_artifact(
        self,
        embedding: list[float] | None,
        artifact_type: str,
        artifact_id: UUID,
        project_key: str | None,
        title: str | None = None,
    ) -> int:
        """Find matching features and create links.

        Returns number of links created. Never raises — logs errors.
        """
        if not embedding or not project_key:
            return 0

        try:
            if self._cluster_guard and title:
                return await self._do_link_via_guard(
                    embedding, artifact_type, artifact_id, project_key, title
                )
            return await self._do_link(embedding, artifact_type, artifact_id, project_key)
        except Exception:
            logger.warning(
                "feature_linker.link_failed",
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                exc_info=True,
            )
            return 0

    # ── ClusterGuard path ────────────────────────────────────────────────

    async def _do_link_via_guard(
        self,
        embedding: list[float],
        artifact_type: str,
        artifact_id: UUID,
        project_key: str,
        title: str,
    ) -> int:
        """Delegate to ClusterGuard, then insert the feature_artifact link."""
        assert self._cluster_guard is not None  # guaranteed by caller

        feature, action = await self._cluster_guard.resolve(
            text=title,
            embedding=embedding,
            project_key=project_key,
            signal_type=artifact_type,
        )

        if feature is None:
            # Link-only mode: no confident candidate for a non-creating
            # signal_type. Nothing to link — this is not an error.
            return 0

        async with self._sf() as session:
            stmt = (
                pg_insert(feature_artifacts)
                .values(
                    feature_id=feature.id,  # type: ignore[attr-defined]
                    artifact_type=artifact_type,
                    artifact_id=artifact_id,
                    similarity_score=1.0,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)
            await session.commit()

        logger.debug(
            "feature_linker.linked_via_guard",
            artifact_type=artifact_type,
            feature_id=str(feature.id),  # type: ignore[attr-defined]
            action=action,
            project_key=project_key,
        )
        return 1

    # ── raw SQL path (backward compat) ───────────────────────────────────

    async def _do_link(
        self,
        embedding: list[float],
        artifact_type: str,
        artifact_id: UUID,
        project_key: str,
    ) -> int:
        similarity_expr = 1 - features.c.embedding.cosine_distance(embedding)

        stmt = select(features.c.id, similarity_expr.label("sim")).where(
            features.c.project_key == project_key,
            features.c.embedding.isnot(None),
            features.c.status != "archived",
            features.c.merged_into.is_(None),
            similarity_expr >= self._threshold,
        )

        async with self._sf() as session:
            rows = (await session.execute(stmt)).fetchall()
            if not rows:
                return 0

            values = [
                {
                    "feature_id": row.id,
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "similarity_score": float(row.sim),
                }
                for row in rows
            ]
            await session.execute(
                pg_insert(feature_artifacts).values(values).on_conflict_do_nothing()
            )
            await session.commit()

            logger.debug(
                "feature_linker.linked",
                artifact_type=artifact_type,
                count=len(rows),
                project_key=project_key,
            )
            return len(rows)
