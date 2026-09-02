"""FeatureLinker — auto-links artifacts to features by embedding similarity.

Called after each artifact insert in the service layer. Queries the features
table for matches (same project_key, cosine similarity >= threshold), then
inserts links into feature_artifacts.

When a ClusterGuard is provided **and** the caller passes a title, the linker
delegates to ClusterGuard.resolve() (which may link, merge, or create a
feature) and then inserts the feature_artifact link.  Otherwise it falls back to
a raw-SQL cosine-similarity path.

DIVERGENCE — the two paths do NOT share a contract, and reading the fallback as
"the same thing, older" is the mistake this note exists to prevent:

    |                     | ClusterGuard path      | raw-SQL fallback     |
    |---------------------|------------------------|----------------------|
    | links per artifact  | at most 1, structural  | up to ``max_links``  |
    | reranker            | yes (0.75 / 0.50)      | none                 |
    | grey zone 0.50–0.70 | arbitrated             | ignored              |
    | link-only mode      | honoured               | unknown              |
    | feature creation    | possible               | never                |

The fallback is not an edge case: ``embedding_backfill`` and the backlog
recovery path both build a linker with no guard at all. Which path ran is
therefore the first question to ask about an unexpected link, which is why
taking the fallback is logged at WARNING and names its reason — a missing guard
and a missing title are two different bugs, in two different files.

The fallback used to have no cap and no ``ORDER BY``: one artifact could attach
itself to every feature above 0.70, in whatever order PostgreSQL felt like.
``max_links`` bounds it, ``ORDER BY sim DESC`` makes the survivors the closest
ones rather than arbitrary ones, and reaching the cap is announced — a silent
truncation reads exactly like "there were only three candidates".

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

#: Cap on links created by the raw-SQL fallback for a single artifact. Matches
#: ``auto_linker``'s default so the two bounded paths of the codebase agree; the
#: ClusterGuard path needs no such constant, being structurally limited to one.
_DEFAULT_MAX_LINKS = 3


class FeatureLinker:
    """Auto-links artifacts to features by cosine similarity."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        threshold: float = _DEFAULT_THRESHOLD,
        cluster_guard: ClusterGuard | None = None,
        max_links: int = _DEFAULT_MAX_LINKS,
    ) -> None:
        self._sf = session_factory
        self._threshold = threshold
        self._cluster_guard = cluster_guard
        self._max_links = max_links

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
            # WARNING, not debug: the fallback has a different contract (see the
            # module docstring), and a debug line is absent in production. The two
            # reasons are kept apart because they are two different bugs — a
            # missing guard is a wiring problem here, a missing title is the
            # CALLER not passing one.
            logger.warning(
                "feature_linker.fallback_path",
                reason="no_cluster_guard" if not self._cluster_guard else "no_title",
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                project_key=project_key,
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
        # ORDER BY before LIMIT, always together: a cap without a sort trades
        # "too many links" for "the wrong links", which is strictly worse.
        stmt = stmt.order_by(similarity_expr.desc()).limit(self._max_links)

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

            if len(rows) == self._max_links:
                # The cap is REACHED, so candidates may have been dropped — we
                # do not know how many, and we do not pretend to. Staying silent
                # would make this page indistinguishable from a project that had
                # only `max_links` features above the threshold.
                logger.warning(
                    "feature_linker.cap_reached",
                    artifact_type=artifact_type,
                    artifact_id=str(artifact_id),
                    max_links=self._max_links,
                    project_key=project_key,
                )
            logger.debug(
                "feature_linker.linked",
                artifact_type=artifact_type,
                count=len(rows),
                project_key=project_key,
            )
            return len(rows)
