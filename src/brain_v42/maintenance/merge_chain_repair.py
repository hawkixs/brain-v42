"""Flatten historical merge pointers without separating audit or projection writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import adrs, consolidation_log, decisions, learnings, runbooks, snippets

MAX_DEPTH = 64
_TABLES = (
    (adrs, "adr"),
    (decisions, "decision"),
    (learnings, "learning"),
    (runbooks, "runbook"),
    (snippets, "snippet"),
)


class MergeChainRepairError(RuntimeError):
    """Expose a content-safe reason when the approved repair cannot proceed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ChainPointer:
    """Retain both old and proposed pointers so an inventory is reviewable."""

    id: UUID
    merged_into: UUID
    root: UUID


@dataclass(frozen=True, slots=True)
class TableReport:
    """Freeze measurements so an operator's approval cannot mutate in memory."""

    table: str
    merged_count: int
    chained_count: int
    transits_other_project: int
    depth_histogram: tuple[tuple[int, int], ...]
    cycles: tuple[UUID, ...]
    dangling_roots: tuple[UUID, ...]
    cross_project_chains: tuple[ChainPointer, ...]
    in_project_chains: tuple[ChainPointer, ...]


@dataclass(frozen=True, slots=True)
class InventoryReport:
    """Keep every table, including empty ones, in the measured approval scope."""

    tables: tuple[TableReport, ...]

    @property
    def in_project_chained_count(self) -> int:
        return sum(len(table.in_project_chains) for table in self.tables)


@dataclass(frozen=True, slots=True)
class RepairReport:
    """Preserve the approved inventory and verification alongside the changed count."""

    before: InventoryReport
    after: InventoryReport
    changed_count: int


async def inventory(session: AsyncSession) -> InventoryReport:
    """Walk each table once, terminating even when historical pointers are corrupt.

    Depth counts edges to the root, so a direct pointer has depth one. Cycles
    identify starting rows whose walk reaches a cycle; dangling roots identify
    absent UUIDs. Neither is eligible for repair. Foreign-project transits count
    starting rows whose path visits another project, regardless of the root;
    eligibility still depends only on the starting and root projects. Table
    names are a fixed local allowlist, never supplied by the operator.
    """
    reports: list[TableReport] = []
    for table, _ in _TABLES:
        statement = sa.text(
            f"""
            WITH RECURSIVE walk AS (
                SELECT source.id, source.merged_into, source.project_key,
                       source.id AS current_id, source.merged_into AS next_id,
                       source.project_key AS current_project,
                       ARRAY[source.id] AS path, 0 AS depth,
                       FALSE AS cycle, FALSE AS dangling,
                       FALSE AS transits_other_project,
                       EXISTS (
                           SELECT 1 FROM {table.name} AS target
                           WHERE target.id = source.merged_into
                             AND target.merged_into IS NOT NULL
                       ) AS chained
                FROM {table.name} AS source
                WHERE source.merged_into IS NOT NULL
                UNION ALL
                SELECT walk.id, walk.merged_into, walk.project_key,
                       walk.next_id, target.merged_into, target.project_key,
                       array_append(walk.path, walk.next_id), walk.depth + 1,
                       walk.next_id = ANY(walk.path), target.id IS NULL,
                       walk.transits_other_project OR (
                           target.id IS NOT NULL
                           AND target.project_key IS DISTINCT FROM walk.project_key
                       ),
                       walk.chained
                FROM walk
                LEFT JOIN {table.name} AS target ON target.id = walk.next_id
                WHERE walk.next_id IS NOT NULL
                  AND NOT walk.cycle AND NOT walk.dangling
                  AND walk.depth < :max_depth
            )
            SELECT * FROM walk
            WHERE next_id IS NULL OR cycle OR dangling OR depth = :max_depth
            ORDER BY id
            """  # noqa: S608  # nosec B608 - fragment = `table.name`, iterated from the module tuple `_TABLES` (five literal tables); no caller or operator input reaches it, and max_depth goes out as the :max_depth bind
        )
        rows = (await session.execute(statement, {"max_depth": MAX_DEPTH})).mappings().all()
        cycles: list[UUID] = []
        dangling: set[UUID] = set()
        cross_project: list[ChainPointer] = []
        in_project: list[ChainPointer] = []
        depths: Counter[int] = Counter()
        for row in rows:
            if row["cycle"]:
                cycles.append(row["id"])
            elif row["dangling"]:
                dangling.add(row["current_id"])
            elif row["next_id"] is not None:
                raise MergeChainRepairError("depth_limit")
            else:
                depths[row["depth"]] += 1
                if row["chained"]:
                    pointer = ChainPointer(row["id"], row["merged_into"], row["current_id"])
                    if row["project_key"] == row["current_project"]:
                        in_project.append(pointer)
                    else:
                        cross_project.append(pointer)
        reports.append(
            TableReport(
                table=table.name,
                merged_count=len(rows),
                chained_count=sum(bool(row["chained"]) for row in rows),
                transits_other_project=sum(bool(row["transits_other_project"]) for row in rows),
                depth_histogram=tuple(sorted(depths.items())),
                cycles=tuple(cycles),
                dangling_roots=tuple(sorted(dangling)),
                cross_project_chains=tuple(cross_project),
                in_project_chains=tuple(in_project),
            )
        )
    return InventoryReport(tuple(reports))


def _refuse_corrupt(report: InventoryReport) -> None:
    """Refuse corruption; foreign keys make dangling roots unreachable today."""
    if any(table.cycles for table in report.tables):
        raise MergeChainRepairError("cycle")
    if any(table.dangling_roots for table in report.tables):
        raise MergeChainRepairError("dangling_root")


async def repair(
    session: AsyncSession, expected: int, *, lock_timeout_ms: int = 5000
) -> RepairReport:
    """Repair only the approved count, leaving commit ownership with the caller.

    The table locks stabilize roots and membership, including concurrent merges
    or inserts that row locks on the candidates alone cannot protect against.
    Acquisition refuses after five seconds by default to bound writer queueing.
    A savepoint also removes all repair writes if the caller catches a refusal.
    The registry pointer trigger stages each graph change in this same
    transaction: it tombstones the old MERGED_INTO edge, upserts the edge to the
    root and writes the matching outbox events.
    """
    if not session.in_transaction():
        raise MergeChainRepairError("transaction_required")
    if expected < 0:
        raise MergeChainRepairError("invalid_expected_count")
    async with session.begin_nested():
        await session.execute(sa.text(f"SET LOCAL lock_timeout = '{lock_timeout_ms / 1000:g}s'"))
        try:
            await session.execute(
                sa.text(
                    "LOCK TABLE "
                    + ", ".join(table.name for table, _ in _TABLES)
                    + " IN SHARE ROW EXCLUSIVE MODE"
                )
            )
        except sa.exc.DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                raise MergeChainRepairError("lock_timeout") from error
            raise
        await session.execute(sa.text("RESET lock_timeout"))
        before = await inventory(session)
        _refuse_corrupt(before)
        if before.in_project_chained_count != expected:
            raise MergeChainRepairError("expected_count_mismatch")

        for (table, entity_type), report in zip(_TABLES, before.tables, strict=True):
            pointers = report.in_project_chains
            if not pointers:
                continue
            for pointer in pointers:
                await session.execute(
                    table.update().where(table.c.id == pointer.id).values(merged_into=pointer.root)
                )
                await session.execute(
                    sa.insert(consolidation_log).values(
                        source_id=pointer.id,
                        target_id=pointer.root,
                        entity_type=entity_type,
                        similarity=1.0,
                        action="repointed",
                    )
                )
        after = await inventory(session)
        _refuse_corrupt(after)
        if after.in_project_chained_count:
            raise MergeChainRepairError("chains_remain")
        return RepairReport(before, after, before.in_project_chained_count)
