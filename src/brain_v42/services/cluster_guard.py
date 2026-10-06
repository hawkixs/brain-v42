"""ClusterGuard — central anti-duplication resolver for feature signals.

Every signal (learning, decision, snippet, MR, push, etc.) passes through
ClusterGuard before creating or linking to a feature. The resolver uses a
resolver that scores on pgvector cosine similarity alone: the top 5 candidates
of the project are ranked by cosine and only the best one is considered.

Thresholds:
    COSINE_LINK      >= 0.70  → link
    COSINE_GREY_LOW  >= 0.50  → grey zone: link at FALLBACK_LINK, else create
    FALLBACK_LINK    >= 0.65  → link inside the grey zone
    below COSINE_GREY_LOW     → create

Why no reranker and no merge: operator ruling d4648d84 (2026-10-06, ticket
ae723cb9) — "ClusterGuard stops using the reranker. Links come from cosine
only and nothing ever merges on a reranker score". A feature's description is
never enriched and never re-embedded here: the signal either links to a
feature, creates one, or is skipped.

Link-only mode: only ``signal_type`` values in ``CREATING_SIGNALS`` may create
a feature. Every other signal type — the five knowledge artifacts
(learning, decision, snippet, runbook, adr) and any unrecognized future
signal — can only link to an existing candidate; when no candidate is
confident enough, ``resolve()`` returns ``(None, "skipped")`` instead of
creating a pseudo-feature. This is a fail-closed allowlist, not a denylist:
an unknown signal_type is treated as knowledge, never as a creation trigger.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import features

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
    from brain_v42.services.status_engine import StatusEngine

logger = structlog.get_logger(__name__)

# ── thresholds ──────────────────────────────────────────────────────────

COSINE_LINK = 0.70
COSINE_GREY_LOW = 0.50
FALLBACK_LINK = 0.65

_TOP_K = 5
_NAME_MAX = 200

# ── link-only allowlist ─────────────────────────────────────────────────
#
# Signal types allowed to create a feature. Everything else (the five
# knowledge artifact types, and any signal_type not listed here) is
# link-only: it may link to a confident candidate but never creates.
# Allowlist, not denylist — fail-closed on unknown signal types.
CREATING_SIGNALS = frozenset(
    {
        "plan",
        "mr_opened",
        "mr_merged",
        "push",
        "pipeline_success",
        "pipeline_failure",
    }
)


def _extract_feature_name(text: str, max_len: int = _NAME_MAX) -> str:
    """First non-empty line of ``text``, stripped, truncated to ``max_len``.

    Artifact bodies frequently start with a title line followed by
    description paragraphs. Slicing the raw text leaks newlines into the
    NOT NULL features.name column and pollutes briefings.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:max_len]
    return "(untitled)"


class ClusterGuard:
    """Anti-duplication resolver for feature signals.

    Decides whether an incoming signal should be linked to an existing
    feature or used to create a brand-new feature. It never merges.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_svc: GPUEmbeddingService,
        status_engine: StatusEngine,
        *,
        mutation_guard: Callable[[], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._status_engine = status_engine
        self._mutation_guard = mutation_guard

    async def resolve(
        self,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
    ) -> tuple[object | None, Literal["linked", "created", "skipped"]]:
        """Resolve a signal to a feature.

        Returns:
            (feature_row, action) where action is one of:
            - "linked":  matched an existing feature (high confidence)
            - "created": inserted a new feature row (CREATING_SIGNALS only)
            - "skipped": link-only signal with no confident candidate;
              feature_row is None
        """
        self._ensure_mutation_allowed()
        async with self._sf() as session:
            candidates = await self._find_candidates(session, embedding, project_key)
            self._ensure_mutation_allowed()

            if not candidates:
                return await self._create_or_skip(
                    session, text, embedding, project_key, signal_type, best_score=None
                )

            best = candidates[0]
            best_score = float(best.similarity)

            # ── high cosine → link directly ────────────────────────────
            if best_score >= COSINE_LINK:
                await self._maybe_update_status(session, best, signal_type)
                await self._commit(session)
                return best, "linked"

            # ── grey zone → cosine-only decision, never a merge ─────────
            if best_score >= COSINE_GREY_LOW:
                return await self._fallback_cosine_only(
                    session, text, embedding, project_key, signal_type, candidates
                )

            # ── low cosine → create new (if allowed) ────────────────────
            return await self._create_or_skip(
                session, text, embedding, project_key, signal_type, best_score=best_score
            )

    # ── internal helpers ────────────────────────────────────────────────

    def _ensure_mutation_allowed(self) -> None:
        if self._mutation_guard is not None:
            self._mutation_guard()

    async def _commit(self, session: AsyncSession) -> None:
        self._ensure_mutation_allowed()
        await session.commit()
        self._ensure_mutation_allowed()

    async def _find_candidates(
        self,
        session: AsyncSession,
        embedding: list[float],
        project_key: str,
    ) -> list:
        """Cosine search for top-K features in the same project.

        Archived features and merged duplicates are excluded: an archived
        cluster must never be a candidate, otherwise _maybe_update_status
        would call StatusEngine.compute_status on it (ValueError pre-fix)
        and the signal would be silently dropped.
        """
        similarity = (1 - features.c.embedding.cosine_distance(embedding)).label("similarity")

        stmt = (
            sa.select(features, similarity)
            .where(
                features.c.project_key == project_key,
                features.c.embedding.isnot(None),
                features.c.status != "archived",
                features.c.merged_into.is_(None),
            )
            .order_by(similarity.desc())
            .limit(_TOP_K)
        )
        result = await session.execute(stmt)
        return list(result.fetchall())

    async def _fallback_cosine_only(
        self,
        session: AsyncSession,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
        candidates: list,
    ) -> tuple[object | None, Literal["linked", "created", "skipped"]]:
        """Decide a grey-zone candidate (0.50 <= cosine < 0.70) on cosine alone.

        >= FALLBACK_LINK (0.65) → link, else create (if allowed). No merge zone.
        """
        best = candidates[0]
        best_score = float(best.similarity)

        if best_score >= FALLBACK_LINK:
            await self._maybe_update_status(session, best, signal_type)
            await self._commit(session)
            return best, "linked"

        return await self._create_or_skip(
            session, text, embedding, project_key, signal_type, best_score=best_score
        )

    def _skip(
        self,
        signal_type: str,
        project_key: str,
        best_score: float | None,
    ) -> tuple[None, Literal["skipped"]]:
        """Log and return the link-only "no creation" outcome."""
        logger.info(
            "cluster_guard.skipped",
            signal_type=signal_type,
            project_key=project_key,
            best_score=best_score,
        )
        return None, "skipped"

    async def _create_or_skip(
        self,
        session: AsyncSession,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
        *,
        best_score: float | None,
    ) -> tuple[object | None, Literal["created", "skipped"]]:
        """Create a feature, unless signal_type is outside CREATING_SIGNALS.

        Link-only mode: knowledge signals (and any unrecognized signal_type,
        fail-closed) never create a feature when no candidate was confident
        enough.
        """
        if signal_type not in CREATING_SIGNALS:
            return self._skip(signal_type, project_key, best_score)

        feature = await self._create_feature(session, text, embedding, project_key, signal_type)
        await self._commit(session)
        return feature, "created"

    async def _create_feature(
        self,
        session: AsyncSession,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
    ) -> object:
        """Insert a new feature row and return it."""
        self._ensure_mutation_allowed()
        initial_status = self._status_engine.compute_status("planned", signal_type, pinned=False)

        stmt = (
            sa.insert(features)
            .values(
                project_key=project_key,
                name=_extract_feature_name(text),
                description=text,
                embedding=embedding,
                status=initial_status,
            )
            .returning(*features.c)
        )
        self._ensure_mutation_allowed()
        result = await session.execute(stmt)
        self._ensure_mutation_allowed()
        return result.fetchone()

    async def _maybe_update_status(
        self,
        session: AsyncSession,
        feature: object,
        signal_type: str,
    ) -> None:
        """Compute new status and update if it progresses."""
        self._ensure_mutation_allowed()
        current = feature.status  # type: ignore[attr-defined]
        pinned = feature.pinned  # type: ignore[attr-defined]
        new_status = self._status_engine.compute_status(current, signal_type, pinned)

        if new_status != current:
            self._ensure_mutation_allowed()
            await session.execute(
                sa.update(features)
                .where(features.c.id == feature.id)  # type: ignore[attr-defined]
                .values(
                    status=new_status,
                    status_updated_at=sa.text("NOW()"),
                    updated_at=sa.text("NOW()"),
                )
            )
            self._ensure_mutation_allowed()
            logger.info(
                "cluster_guard.status_updated",
                feature_id=str(feature.id),  # type: ignore[attr-defined]
                old_status=current,
                new_status=new_status,
                signal_type=signal_type,
            )
