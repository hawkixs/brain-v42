"""DecayFlusher — periodic background task for access_log aggregation + freshness updates.

Follows the MetricsFlusher pattern: asyncio.create_task with start()/stop().
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import adrs, decisions, indexed_plans, learnings, runbooks, snippets
from brain_v42.services.decay import DecayCalculator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.repositories.pg_access_log import PgAccessLogRepo

logger = structlog.get_logger(__name__)

# Map entity_type string to SQLAlchemy table
_ENTITY_TABLES: dict[str, sa.Table] = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
    "plan": indexed_plans,
}


class DecayFlusher:
    """Periodically aggregates access_log and updates entity freshness status."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        access_log_repo: PgAccessLogRepo,
        decay_calculator: DecayCalculator,
        interval_seconds: int = 300,
        collector: Any | None = None,
        human_signal_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._access_log_repo = access_log_repo
        self._decay_calculator = decay_calculator
        self._interval = interval_seconds
        self._collector = collector
        # Défaut FERMÉ dans la signature, pas seulement dans Settings : un
        # appelant qui oublie de passer le réglage obtient le comportement
        # d'aujourd'hui, jamais le nouveau.
        self._human_signal_enabled = human_signal_enabled
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the periodic flusher."""
        self._task = asyncio.create_task(self._run_loop())
        logger.info("decay_flusher.started", interval=self._interval)

    async def stop(self) -> None:
        """Stop the flusher."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("decay_flusher.stopped")

    async def _run_loop(self) -> None:
        """Main loop: flush every interval."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("decay_flusher.error")

    async def _flush(self) -> None:
        """Aggregate access_log, update freshness, purge old entries.

        All three operations (advisory-lock acquisition, access_log aggregate
        + DELETE, and entity freshness UPDATEs) run inside a **single
        transaction** so a mid-flush crash cannot lose access counts.

        Transaction ownership:
          - This method opens the session and calls session.commit().
          - PgAccessLogRepo.aggregate_in_session() writes inside the same
            session without committing — the lock, aggregate, and DELETE are
            deferred to the final commit here.
        """
        # 0. Snapshot queue size BEFORE aggregation drains it (for metrics)
        access_log_size = 0
        if self._collector is not None:
            try:
                access_log_size = await self._access_log_repo.row_count()
            except Exception:
                logger.exception("decay_flusher.row_count_failed")

        # 1+2. Open ONE session: aggregate access_log (advisory lock + DELETE
        #      deferred) and update entity freshness — all in the same txn.
        async with self._session_factory() as session:
            aggregated = await self._access_log_repo.aggregate_in_session(session)

            if aggregated:
                by_type: dict[str, dict[Any, dict[str, Any]]] = {}
                for (entity_type, entity_id), stats in aggregated.items():
                    by_type.setdefault(entity_type, {})[entity_id] = stats
                for entity_type, id_stats in by_type.items():
                    await self._update_entities_batch(session, entity_type, id_stats)

            await session.commit()

        # 3. Purge old access_log entries (separate, best-effort)
        await self._access_log_repo.purge_old(30)

        # 4. Report decay stats to the collector (if wired)
        if self._collector is not None:
            try:
                stale, archived = await self._count_freshness_totals()
                self._collector.record_decay_stats(
                    stale_count=stale,
                    archived_count=archived,
                    access_log_size=access_log_size,
                )
            except Exception:
                logger.exception("decay_flusher.record_stats_failed")

    async def _count_freshness_totals(self) -> tuple[int, int]:
        """Sum stale + archived counts across all decay-tracked entity tables."""
        total_stale = 0
        total_archived = 0
        async with self._session_factory() as session:
            for table in _ENTITY_TABLES.values():
                stmt = sa.select(
                    sa.func.count().filter(table.c.freshness_status == "stale").label("stale"),
                    sa.func.count()
                    .filter(table.c.freshness_status == "archived")
                    .label("archived"),
                ).select_from(table)
                row = (await session.execute(stmt)).one()
                total_stale += row[0] or 0
                total_archived += row[1] or 0
        return total_stale, total_archived

    async def _update_entities_batch(
        self,
        session: AsyncSession,
        entity_type: str,
        id_stats: dict[Any, dict[str, Any]],
    ) -> None:
        """Batch-update entities of a single type: 1 SELECT + N UPDATEs."""
        table = _ENTITY_TABLES.get(entity_type)
        if table is None:
            return

        ids = list(id_stats.keys())

        # Select ONLY needed columns — skip embedding, search_vector, and content
        select_cols: list[sa.ColumnElement[Any]] = [
            table.c.id,
            table.c.created_at,
            table.c.access_count,
            table.c.access_count_human,
            table.c.freshness_status,
            table.c.last_accessed_at,
            table.c.last_accessed_at_human,
        ]
        if "validated_at" in table.c:
            select_cols.append(table.c.validated_at)
        if "decided_at" in table.c:
            select_cols.append(table.c.decided_at)

        stmt = sa.select(*select_cols).where(table.c.id.in_(ids))
        result = await session.execute(stmt)
        rows = {row["id"]: row for row in result.mappings().all()}

        # Compute updates in Python, then issue bulk UPDATEs
        status_changed: list[dict[str, Any]] = []
        status_same: list[dict[str, Any]] = []

        for entity_id, stats in id_stats.items():
            row = rows.get(entity_id)
            if row is None:
                continue

            new_access_count = row["access_count"] + stats["count"]
            new_access_count_human = row["access_count_human"] + stats.get("count_human", 0)
            new_last_accessed = stats["max_accessed"]
            # `None` = aucune lecture HUMAINE dans ce lot. On garde alors la
            # valeur existante : un lot machine ne doit pas effacer la dernière
            # trace humaine, ce serait le signal contaminé dans l'autre sens.
            batch_human = stats.get("max_accessed_human")
            new_last_accessed_human = (
                batch_human if batch_human is not None else row["last_accessed_at_human"]
            )

            is_validated = False
            if "validated_at" in table.c and row["validated_at"] is not None:
                is_validated = True
            elif "decided_at" in table.c and row["decided_at"] is not None:
                is_validated = True

            # §5.5 — le seul changement de ce chantier qu'un humain sentirait
            # le jour même, donc derrière un réglage, valeurs d'aujourd'hui par
            # défaut. Fermé, les deux signaux restent les totaux : comportement
            # inchangé, à l'octet près.
            if self._human_signal_enabled:
                signal_last_accessed = new_last_accessed_human
                signal_access_count = new_access_count_human
            else:
                signal_last_accessed = new_last_accessed
                signal_access_count = new_access_count

            multiplier = self._decay_calculator.compute_multiplier(
                entity_type=entity_type,
                created_at=row["created_at"],
                last_accessed_at=signal_last_accessed,
                access_count=signal_access_count,
                is_validated=is_validated,
            )

            new_status = self._decay_calculator.freshness_status(multiplier)
            old_status = row["freshness_status"]

            params: dict[str, Any] = {
                "_entity_id": entity_id,
                "access_count": new_access_count,
                "access_count_human": new_access_count_human,
                "last_accessed_at": new_last_accessed,
                "last_accessed_at_human": new_last_accessed_human,
            }

            if new_status != old_status:
                params["freshness_status"] = new_status
                logger.info(
                    "freshness_transition",
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    old=old_status,
                    new=new_status,
                    multiplier=round(multiplier, 3),
                )
                status_changed.append(params)
            else:
                status_same.append(params)

        # Bulk UPDATE — one statement per group (different column sets).
        # NOTE: distinct variable names from the earlier `stmt = sa.select(...)`
        # to avoid mypy "Update vs Select[Any]" assignment narrowing error.
        if status_same:
            upd_same = (
                sa.update(table)
                .where(table.c.id == sa.bindparam("_entity_id"))
                .values(
                    access_count=sa.bindparam("access_count"),
                    access_count_human=sa.bindparam("access_count_human"),
                    last_accessed_at=sa.bindparam("last_accessed_at"),
                    last_accessed_at_human=sa.bindparam("last_accessed_at_human"),
                )
            )
            await session.execute(upd_same, status_same)

        if status_changed:
            upd_changed = (
                sa.update(table)
                .where(table.c.id == sa.bindparam("_entity_id"))
                .values(
                    access_count=sa.bindparam("access_count"),
                    access_count_human=sa.bindparam("access_count_human"),
                    last_accessed_at=sa.bindparam("last_accessed_at"),
                    last_accessed_at_human=sa.bindparam("last_accessed_at_human"),
                    freshness_status=sa.bindparam("freshness_status"),
                    # Migration 043 : le trigger DATE la transition, mais il ne
                    # peut pas savoir d'où elle vient. Le flusher, lui, le sait —
                    # c'est le calcul du score. Sans cette déclaration, le
                    # trigger remettrait la source à NULL, et la colonne ne
                    # dirait jamais rien de l'écrivain le plus fréquent.
                    freshness_source=sa.literal("score"),
                )
            )
            await session.execute(upd_changed, status_changed)
