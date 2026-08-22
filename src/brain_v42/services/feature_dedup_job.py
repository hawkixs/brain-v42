"""FeatureDedupJob — periodic feature deduplication using cosine pre-filter + cross-encoder.

Scans all features in a project, finds near-duplicate pairs using a two-stage
pipeline (pgvector cosine similarity pre-filter, then cross-encoder reranker),
and merges confirmed duplicates (oldest absorbs newest).  The absorbed feature
is archived with a ``merged_into`` pointer so roadmap history remains auditable.

Usage:
    job = FeatureDedupJob(session_factory, reranker, embedding_svc)
    candidates = await job.find_candidates("brain_v42")
    for target, source, score in candidates:
        async with session_factory() as session:
            await job.merge_features(session, target, source)
            await session.commit()
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import feature_artifacts, features, gitlab_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
    from brain_v42.services.reranker_client import RerankerClient

logger = structlog.get_logger(__name__)

# ── thresholds ──────────────────────────────────────────────────────────

COSINE_PREFILTER = 0.50
RERANKER_MERGE_THRESHOLD = 0.80
_TOP_K_NEIGHBORS = 3


class FeatureDedupJob:
    """Periodic feature deduplication using cosine pre-filter + cross-encoder reranker.

    Pipeline:
    1. Get all features for a project with embeddings
    2. For each feature, find top-3 neighbors via cosine similarity (>= 0.50)
    3. Run cross-encoder on pre-filtered pairs
    4. Score >= 0.80 -> candidate for merge (oldest absorbs newest)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        reranker: RerankerClient,
        embedding_svc: GPUEmbeddingService,
        *,
        mutation_guard: Callable[[], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._reranker = reranker
        self._embedding_svc = embedding_svc
        self._mutation_guard = mutation_guard

    async def find_candidates(
        self,
        project_key: str,
    ) -> list[tuple[Any, Any, float]]:
        """Find duplicate feature pairs using cosine pre-filter + cross-encoder.

        Returns:
            List of (target, source, score) tuples where target is the oldest
            feature that absorbs the newest (source). Score is the reranker score.
        """
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

                    # `pinned` marque un engagement explicite de l'opérateur, et
                    # la source est celle qui DISPARAÎT. Absorber une épinglée
                    # détruit donc un engagement — c'est arrivé le 2026-08-14 à
                    # 19:17 sur une feature créée à la main, avec un
                    # reranker_score de 0,83 obtenu en ne comparant que les NOMS,
                    # jamais les descriptions qui portent le périmètre.
                    #
                    # On refuse la paire au lieu d'échanger les rôles : faire
                    # absorber par l'épinglée déciderait du survivant sur
                    # l'épinglage plutôt que sur l'âge, et fusionnerait quand
                    # même deux périmètres que rien ne prouve identiques. Une
                    # épinglée en CIBLE reste autorisée, c'est le cas nominal.
                    #
                    # `bool()` et non `is True` : la colonne est nullable, et
                    # NULL veut dire « pas épinglée », pas « inconnu ».
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

                # Step 4: score >= threshold -> candidate
                if reranker_score >= RERANKER_MERGE_THRESHOLD:
                    candidates.append((target, source, reranker_score))
                    logger.info(
                        "feature_dedup.candidate_found",
                        target_id=str(target.id),
                        source_id=str(source.id),
                        reranker_score=reranker_score,
                    )

            return candidates

    async def merge_features(
        self,
        session: AsyncSession,
        target: Any,
        source: Any,
    ) -> bool:
        """Merge source into target: transfer artifacts, archive source.

        Returns:
            True  — merge was performed.
            False — merge was skipped (one or both rows no longer exist).

        Steps:
        0. Re-SELECT target and source FOR UPDATE to verify both still exist.
           If either is missing or no longer a live merge root, skip the merge
           and emit a warning.  This prevents stale candidates from merging an
           archived row back into its own survivor.
        1. Enrich target description with source description — read from the
           authoritative FOR UPDATE rows, not from the stale snapshot objects
           passed as arguments (which may reflect a pre-merge state).
        2. Re-embed target via embedding_svc.embed(); on failure, KEEP the
           existing target embedding (do NOT write None — that would make the
           feature permanently invisible in cosine searches).
        3. Transfer all feature_artifacts from source to target.
        4. Transfer all gitlab_events from source to target (avoids FK violation).
        5. Re-parent features previously merged into source (self-FK without cascade).
        6. Update target row (description + embedding).
        7. Archive source feature with ``merged_into=target``.
        8. Log that the merge is staged; the scheduler logs durable success only
           after commit.
        """
        target_id = target.id
        source_id = source.id

        if target_id == source_id:
            logger.warning(
                "feature_dedup.merge_skipped_same_id",
                feature_id=str(target_id),
            )
            return False

        # 0. Existence re-check with FOR UPDATE — serializes concurrent merges
        #    and guards against chain-of-merges where source was already deleted.
        self._ensure_mutation_allowed()
        recheck_result = await session.execute(
            sa.select(features).where(features.c.id.in_([target_id, source_id])).with_for_update()
        )
        self._ensure_mutation_allowed()
        found_rows = recheck_result.fetchall()
        found_ids = {row.id for row in found_rows}

        if target_id not in found_ids or source_id not in found_ids:
            missing = []
            if target_id not in found_ids:
                missing.append(f"target={target_id}")
            if source_id not in found_ids:
                missing.append(f"source={source_id}")
            logger.warning(
                "feature_dedup.merge_skipped_missing",
                missing=missing,
                target_id=str(target_id),
                source_id=str(source_id),
            )
            return False

        # Read descriptions and embedding from the authoritative FOR UPDATE rows,
        # not from the stale snapshot objects passed as arguments.  The snapshot
        # may reflect a pre-merge state (e.g. target.description lacks previously
        # absorbed text after a prior merge in the same run).
        target_row = next(r for r in found_rows if r.id == target_id)
        source_row = next(r for r in found_rows if r.id == source_id)
        if (
            target_row.status == "archived"
            or target_row.merged_into is not None
            or source_row.status == "archived"
            or source_row.merged_into is not None
        ):
            logger.warning(
                "feature_dedup.merge_skipped_not_live_root",
                target_id=str(target_id),
                target_status=target_row.status,
                target_merged_into=str(target_row.merged_into)
                if target_row.merged_into is not None
                else None,
                source_id=str(source_id),
                source_status=source_row.status,
                source_merged_into=str(source_row.merged_into)
                if source_row.merged_into is not None
                else None,
            )
            return False
        # `find_candidates` filtre déjà les sources épinglées, mais ce filtre vit
        # dans le chemin de DÉCOUVERTE. Ici est le chemin de MUTATION, et c'est le
        # seul endroit où l'invariant peut vraiment tenir :
        #
        # - `run_dedup_loop` collecte TOUS les candidats d'un projet, puis les
        #   fusionne un par un, chacun dans sa propre session et après un
        #   aller-retour reranker. Un humain qui épingle pendant cette fenêtre
        #   verrait son geste ignoré, la décision ayant été prise sur un
        #   instantané d'avant.
        # - `merge_features` est publique et le docstring du module la documente
        #   comme appelable directement. Un tel appelant n'hérite d'aucune garde.
        #
        # D'où la lecture sur `source_row`, la ligne relue FOR UPDATE, et JAMAIS
        # sur l'argument `source` : l'instantané est précisément ce qui peut être
        # périmé.
        #
        # ON BLOQUE, ON N'INVERSE PAS. Échanger les rôles ferait décider du
        # survivant par l'épinglage plutôt que par l'âge, et fusionnerait quand
        # même deux périmètres que rien ne prouve identiques — le score vient du
        # reranker sur les NOMS seuls. Même choix que `find_candidates`, pour que
        # les deux chemins ne racontent pas deux histoires. Et un primitif de
        # mutation qui ferait silencieusement autre chose que ce qu'on lui demande
        # serait pire ici que dans un filtre.
        #
        # Les DEUX épinglées est un sous-cas de celui-ci, donc bloqué aussi. Il est
        # journalisé à part : rien ne dit laquelle des deux intentions doit céder,
        # c'est un arbitrage humain, pas une règle à écrire dans le code.
        #
        # `bool()` et non `is True` : la colonne est nullable (`server_default
        # false`), et NULL veut dire « pas épinglée », pas « inconnu ».
        if bool(source_row.pinned):
            logger.warning(
                "feature_dedup.merge_skipped_pinned_source",
                target_id=str(target_id),
                source_id=str(source_id),
                both_pinned=bool(target_row.pinned),
            )
            return False

        target_desc: Any = target_row.description
        source_desc: Any = source_row.description
        existing_embedding: Any = target_row.embedding

        # 1. Enrich and re-embed before staging DML.  The authoritative feature
        #    row locks remain held, but artifact/event locks are not held across
        #    the remote embedding call.
        enriched_desc = f"{target_desc}\n---\n{source_desc}"
        new_embedding: Any = existing_embedding
        try:
            new_embedding = await self._embedding_svc.embed(enriched_desc)
        except Exception:
            logger.warning(
                "feature_dedup.embed_failed_keeping_existing",
                target_id=str(target_id),
                source_id=str(source_id),
                exc_info=True,
            )
        # This guard must remain outside the best-effort embedding exception
        # handler so OwnershipLostError cannot be swallowed.
        self._ensure_mutation_allowed()

        # 2. Transfer artifacts
        self._ensure_mutation_allowed()
        await session.execute(
            sa.update(feature_artifacts)
            .where(feature_artifacts.c.feature_id == source_id)
            .values(feature_id=target_id)
        )
        self._ensure_mutation_allowed()

        # 3. Transfer gitlab_events — without this the final DELETE trips
        # the gitlab_events.feature_id FK (ON DELETE NO ACTION) whenever the
        # source has any webhook event attached, silently rolling back the
        # whole merge transaction.
        self._ensure_mutation_allowed()
        await session.execute(
            sa.update(gitlab_events)
            .where(gitlab_events.c.feature_id == source_id)
            .values(feature_id=target_id)
        )
        self._ensure_mutation_allowed()

        # 4. Flatten archived/merged descendants onto the surviving target.
        self._ensure_mutation_allowed()
        await session.execute(
            sa.update(features)
            .where(features.c.merged_into == source_id)
            .values(merged_into=target_id)
        )
        self._ensure_mutation_allowed()

        # 5. Update target
        self._ensure_mutation_allowed()
        await session.execute(
            sa.update(features)
            .where(features.c.id == target_id)
            .values(
                description=enriched_desc,
                embedding=new_embedding,
                updated_at=sa.text("NOW()"),
            )
        )
        self._ensure_mutation_allowed()

        # 6. Preserve the source as an auditable archive.  Roadmap curation
        #    proposals reference feature rows with ON DELETE CASCADE, so a
        #    physical DELETE would erase their history.
        self._ensure_mutation_allowed()
        await session.execute(
            sa.update(features)
            .where(features.c.id == source_id)
            .values(
                status="archived",
                merged_into=target_id,
                status_updated_at=sa.text("NOW()"),
                updated_at=sa.text("NOW()"),
            )
        )
        self._ensure_mutation_allowed()

        # 7. The transaction is still pending; durable success is logged by the
        # scheduler only after its guarded commit.
        logger.info(
            "feature_dedup.merge_staged",
            target_id=str(target_id),
            source_id=str(source_id),
            target_name=target.name,
            source_name=source.name,
        )

        return True

    # ── internal helpers ────────────────────────────────────────────────

    def _ensure_mutation_allowed(self) -> None:
        if self._mutation_guard is not None:
            self._mutation_guard()

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
