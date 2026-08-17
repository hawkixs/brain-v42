"""PostgreSQL repository for Decision entities.

Implements:
- CRUD (create, get_by_id, list_all, update, delete)
- Full-text search on title/description/reasoning/tags via tsvector
- Vector similarity search via pgvector (cosine distance)
- get_supersession_chain via recursive CTE
- supersede — create new + mark old as superseded in one transaction
- Combined filters: project_key, status, tags

Design (vague 3 re-platform):
- CRUD/FTS/vector delegate to BasePgRepository.
- Row → model conversion happens here via _row_to_decision().
- KEEP verbatim: delete (superseded_by ref clearing), supersede (2-statement
  txn), get_supersession_chain (wave-2 CTE cap), fetch_cross_project_resonance_pairs.
- Assumed behavior change: update() now stamps updated_at client-side
  (datetime.now(UTC)) instead of server-side NOW() — aligned with snippet/runbook.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import decisions
from brain_v42.models.decision import Decision, DecisionCreate, DecisionUpdate
from brain_v42.repositories.pg_base import BasePgRepository, Row, project_scope

logger = structlog.get_logger(__name__)


class PgDecisionRepo(BasePgRepository):
    """Async repository for the `decisions` table.

    Extends BasePgRepository with Decision-specific methods:
    - create/get_by_id/list_all/update/delete (CRUD)
    - search_fts (full-text search via tsvector)
    - search_vector (semantic search via pgvector cosine distance)
    - get_supersession_chain (recursive CTE)
    - supersede (atomic INSERT + UPDATE transaction)
    - fetch_cross_project_resonance_pairs (cross-project resonance SQL)
    """

    table = decisions
    fts_columns: list[str] = []  # search_vector is DB-generated

    _STRIP_COLS = frozenset(("search_vector", "rank", "similarity", "distance"))

    # ── Row → model conversion ─────────────────────────────────────────────

    @staticmethod
    def _row_to_decision(row: Row | Any) -> Decision:
        """Convert a DB row (dict or mapping) to a Decision model.

        Strips generated columns: search_vector, rank, similarity, distance.
        """
        data = {k: v for k, v in dict(row).items() if k not in PgDecisionRepo._STRIP_COLS}
        return Decision.model_validate(data)

    # Alias used by BasePgRepository convention (each subclass exposes _row_to_model).
    def _row_to_model(self, row: Row) -> Decision:
        return self._row_to_decision(row)

    # ── Payload builders (shared by create + supersede) ────────────────────

    @staticmethod
    def _create_payload(
        data: DecisionCreate,
        embedding: list[float] | None = None,
        *,
        id: UUID | None = None,
    ) -> dict[str, Any]:
        """Build an INSERT payload dict from a DecisionCreate model."""
        return {
            "id": id if id is not None else uuid.uuid4(),
            "title": data.title,
            "description": data.description,
            "reasoning": data.reasoning,
            "alternatives": data.alternatives,
            "consequences": data.consequences,
            "project_key": data.project_key,
            "tags": data.tags,
            "status": data.status,
            "superseded_by": None,
            "embedding": embedding,
            "metadata": data.metadata,
        }

    # ── CREATE ─────────────────────────────────────────────────────────────

    async def create(  # type: ignore[override]
        self,
        data: DecisionCreate,
        embedding: list[float] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> Decision:
        """Insert a new decision row. Returns the created Decision."""
        payload = self._create_payload(data, embedding)
        row = await super().create(payload, session=session)
        logger.debug("pg_decision.create", id=str(payload["id"]))
        return self._row_to_decision(row)

    # ── READ ───────────────────────────────────────────────────────────────

    async def get_by_id(  # type: ignore[override]
        self,
        decision_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Decision | None:
        """Fetch a single decision by UUID. Returns None if not found."""
        if project_key is None:
            row = await super().get_by_id(decision_id, session=session)
        else:
            row = await self.find_one(
                {"id": decision_id, "project_key": project_key},
                session=session,
            )
        return self._row_to_decision(row) if row is not None else None

    async def list_all(  # type: ignore[override]
        self,
        *,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        include_archived: bool = False,
        session: AsyncSession | None = None,
    ) -> list[Decision]:
        """List decisions with optional AND-combined filters.

        Tags filter uses && (overlap) operator — any matching tag returns the row.
        When include_archived=False (default), excludes merged (merged_into IS NOT NULL)
        and archived (freshness_status = 'archived') decisions.
        """
        scope = project_scope(project_key, project_keys)
        filters: dict[str, Any] = {
            "project_key": scope,
            "status": status,
        }
        extra_clauses = []
        tags_clause = self._tags_clause(self.table.c.tags, tags)
        if tags_clause is not None:
            extra_clauses.append(tags_clause)

        rows, _total = await super().list_all(
            offset=offset,
            limit=limit,
            filters=filters,
            order_by="created_at",
            order_desc=True,
            skip_count=True,
            include_archived=False if not include_archived else None,
            extra_clauses=extra_clauses if extra_clauses else None,
            session=session,
        )
        return [self._row_to_decision(r) for r in rows]

    # ── UPDATE ─────────────────────────────────────────────────────────────

    async def update(  # type: ignore[override]
        self,
        decision_id: UUID,
        data: DecisionUpdate,
        embedding: list[float] | None = None,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> Decision | None:
        """Partial update — only non-None fields are applied.

        Behavior change (assumed, vague 3): updated_at is now stamped
        client-side via datetime.now(UTC) instead of server-side NOW().
        Returns None when the decision_id doesn't exist.
        """
        values: dict[str, Any] = data.model_dump(exclude_none=True)
        if embedding is not None:
            values["embedding"] = embedding
        if not values:
            # No-op: refetch current state
            if project_key is None:
                return await self.get_by_id(decision_id, session=session)
            return await self.get_by_id(
                decision_id,
                project_key=project_key,
                session=session,
            )
        if project_key is None:
            row = await super().update(decision_id, values, session=session)
        else:
            stmt = (
                decisions.update()
                .where(
                    decisions.c.id == decision_id,
                    decisions.c.project_key == project_key,
                )
                .values(**values, updated_at=datetime.now(UTC))
                .returning(decisions)
            )
            async with self._maybe_session(session, write=True) as sess:
                result = await sess.execute(stmt)
                mapping = result.mappings().one_or_none()
                row = dict(mapping) if mapping is not None else None
        return self._row_to_decision(row) if row is not None else None

    # ── DELETE ─────────────────────────────────────────────────────────────

    async def delete(  # type: ignore[override]
        self,
        decision_id: UUID,
        *,
        project_key: str | None = None,
        session: AsyncSession | None = None,  # noqa: ARG002 — unused, kept for interface
    ) -> bool:
        """Delete a decision. Returns True if row existed, False otherwise.

        Before deleting, clears superseded_by references from other decisions
        that point to this one and resets their status to 'active'.
        This prevents FK violation when deleting a superseding decision.

        Uses transaction() so both statements land in the same atomic commit.
        """
        if project_key is not None:
            async with self.transaction(session) as sess:
                target_stmt = (
                    sa.select(decisions.c.id)
                    .where(
                        decisions.c.id == decision_id,
                        decisions.c.project_key == project_key,
                    )
                    .with_for_update()
                )
                target = (await sess.execute(target_stmt)).scalar_one_or_none()
                if target is None:
                    return False

                reference_locks_stmt = (
                    sa.select(decisions.c.id, decisions.c.project_key)
                    .where(decisions.c.superseded_by == decision_id)
                    .with_for_update()
                )
                reference_rows = (await sess.execute(reference_locks_stmt)).mappings().all()
                if any(row["project_key"] != project_key for row in reference_rows):
                    return False

                clear_stmt = (
                    decisions.update()
                    .where(
                        decisions.c.superseded_by == decision_id,
                        decisions.c.project_key == project_key,
                    )
                    .values(superseded_by=None, status="active")
                )
                await sess.execute(clear_stmt)
                delete_stmt = (
                    decisions.delete()
                    .where(
                        decisions.c.id == decision_id,
                        decisions.c.project_key == project_key,
                    )
                    .returning(decisions.c.id)
                )
                deleted = (await sess.execute(delete_stmt)).scalar_one_or_none()
                return deleted is not None

        async with self.transaction() as sess:
            # Clear superseded_by refs pointing to this decision
            clear_stmt = (
                decisions.update()
                .where(decisions.c.superseded_by == decision_id)
                .values(superseded_by=None, status="active")
            )
            await sess.execute(clear_stmt)
            # Delegate the actual DELETE to the base
            return await super().delete(decision_id, session=sess)

    # ── FULL-TEXT SEARCH ───────────────────────────────────────────────────

    async def search_fts(  # type: ignore[override]
        self,
        query: str,
        *,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        session: AsyncSession | None = None,
    ) -> list[tuple[Decision, float]]:
        """Full-text search using tsvector search_vector column with ts_rank scoring.

        Returns list of (Decision, rank_score) tuples ordered by relevance DESC.
        NOTE: does not filter archived rows — brain_service refilters in Python.
        """
        scope = project_scope(project_key, project_keys)
        filters: dict[str, Any] = {
            "project_key": scope,
            "status": status,
        }
        extra_clauses = []
        tags_clause = self._tags_clause(self.table.c.tags, tags)
        if tags_clause is not None:
            extra_clauses.append(tags_clause)

        rows, _total = await super().search_fts(
            query,
            filters=filters,
            offset=offset,
            limit=limit,
            skip_count=True,
            extra_clauses=extra_clauses if extra_clauses else None,
            session=session,
        )
        return [(self._row_to_decision(r), float(r.get("rank", 0.0))) for r in rows]

    # ── VECTOR SEARCH ──────────────────────────────────────────────────────

    async def search_vector(  # type: ignore[override]
        self,
        query_embedding: list[float],
        *,
        project_key: str | None = None,
        project_keys: list[str] | None = None,
        limit: int = 20,
        session: AsyncSession | None = None,
    ) -> list[tuple[Decision, float]]:
        """Semantic search using pgvector cosine distance (<=> operator).

        Returns list of (Decision, similarity) tuples where similarity = 1 - distance.
        Results ordered by distance ASC (most similar first).
        Default limit=20 (same public default as before re-platform).
        """
        scope = project_scope(project_key, project_keys)
        filters: dict[str, Any] = {
            "project_key": scope,
        }

        rows = await super().search_vector(
            query_embedding,
            filters=filters,
            limit=limit,
            session=session,
        )
        return [(self._row_to_decision(r), float(r.get("similarity", 0.0))) for r in rows]

    # ── Cross-project resonance (Spec C MVP β) ─────────────────────────────

    async def fetch_cross_project_resonance_pairs(
        self, *, ids: list[str], threshold: float
    ) -> list[dict]:
        """All cross-project decision pairs above cosine threshold, via pgvector.

        Pair compute stays in PG (no embedding payload crosses to Python).
        Caller bounds `ids` (MAX_DECISIONS_PER_DOMAIN cap upstream).
        ids are UUID strings; the explicit uuid[] cast keeps asyncpg's array
        binding unambiguous through sa.text.
        Returns plain row dicts; the resonance script maps them to ResonancePair.
        """
        if not ids:
            return []
        query = sa.text(
            """
            SELECT
                a.id AS a_id, b.id AS b_id,
                a.project_key AS a_project, b.project_key AS b_project,
                a.title AS a_title, b.title AS b_title,
                a.created_at::date AS a_created_at, b.created_at::date AS b_created_at,
                (1 - (a.embedding <=> b.embedding))::float AS cosine
            FROM decisions a
            JOIN decisions b ON a.id < b.id
            WHERE a.id = ANY(CAST(:ids AS uuid[])) AND b.id = ANY(CAST(:ids AS uuid[]))
              AND a.project_key <> b.project_key
              AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
              AND (1 - (a.embedding <=> b.embedding)) >= :threshold
            ORDER BY cosine DESC
            """
        )
        async with self.get_session() as sess:
            result = await sess.execute(query, {"ids": list(ids), "threshold": threshold})
            return [dict(r) for r in result.mappings().all()]

    # ── SUPERSESSION CHAIN ─────────────────────────────────────────────────

    async def get_supersession_chain(self, decision_id: UUID) -> list[Decision]:
        """Walk the full supersession chain bidirectionally using recursive CTEs.

        Two-phase approach:
        1. backward CTE: walk new→old (find decisions whose superseded_by
           points to us) to locate the chain root (oldest decision).
        2. forward CTE: walk old→new from root via superseded_by links.

        Returns decisions ordered oldest→newest regardless of which
        decision in the chain is passed as decision_id.
        UUID is passed as string for sa.text() parameter compatibility.
        """
        # Depth cap: prevents infinite recursion when a cycle exists in superseded_by.
        # READ COMMITTED does not prevent application-level cycles; we cap at 50
        # hops (sane maximum for a real supersession chain) in both CTEs.
        stmt = sa.text("""
            WITH RECURSIVE
            backward AS (
                SELECT id, superseded_by, 0 AS depth
                FROM decisions
                WHERE id = :decision_id
                UNION ALL
                SELECT d.id, d.superseded_by, b.depth - 1
                FROM decisions d
                JOIN backward b ON d.superseded_by = b.id
                WHERE b.depth > -50
            ),
            forward AS (
                SELECT id, title, description, reasoning, alternatives,
                       consequences, project_key, tags, status, superseded_by,
                       metadata, created_at, updated_at, 1 AS depth
                FROM decisions
                WHERE id = (SELECT id FROM backward ORDER BY depth LIMIT 1)
                UNION ALL
                SELECT d.id, d.title, d.description, d.reasoning,
                       d.alternatives, d.consequences, d.project_key, d.tags,
                       d.status, d.superseded_by, d.metadata,
                       d.created_at, d.updated_at, f.depth + 1
                FROM decisions d
                JOIN forward f ON d.id = f.superseded_by
                WHERE f.depth < 50
            )
            SELECT id, title, description, reasoning, alternatives, consequences,
                   project_key, tags, status, superseded_by, metadata,
                   created_at, updated_at
            FROM forward
            ORDER BY depth
            LIMIT 50
        """)
        async with self.get_session() as sess:
            result = await sess.execute(stmt, {"decision_id": str(decision_id)})
            rows = result.mappings().all()
            return [Decision.model_validate(dict(row)) for row in rows]

    # ── SUPERSEDE ─────────────────────────────────────────────────────────

    async def supersede(
        self,
        old_id: UUID,
        new_data: DecisionCreate,
        new_embedding: list[float] | None = None,
    ) -> Decision:
        """Create a new decision and mark old_id as superseded — single transaction.

        Steps (atomic via transaction()):
        1. INSERT new decision row via super().create(session=s)
        2. UPDATE old decision: status='superseded', superseded_by=new_id
        3. COMMIT once (via transaction() context manager exit)

        Returns the newly created Decision.
        """
        new_id = uuid.uuid4()
        payload = self._create_payload(new_data, new_embedding, id=new_id)
        update_stmt = (
            decisions.update()
            .where(decisions.c.id == old_id)
            .values(
                status="superseded",
                superseded_by=new_id,
                updated_at=sa.text("NOW()"),
            )
        )
        async with self.transaction() as sess:
            row = await super().create(payload, session=sess)
            await sess.execute(update_stmt)
            logger.debug("pg_decision.supersede", old_id=str(old_id), new_id=str(new_id))
            return self._row_to_decision(row)
