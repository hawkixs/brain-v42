"""Bounded processing of the durable PostgreSQL embedding backlog."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import _EMBEDDING_DIM, MIN_COMPARABLE_EMBEDDING_NORM
from brain_v42.services.embedding_text import EmbeddingEntityType, embedding_text_from_row
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable
from brain_v42.services.graph_helpers import auto_link_if_enabled, link_artifact_if_enabled

logger = structlog.get_logger(__name__)

EMBEDDING_BACKFILL_LOCK = 0x425241494E454D42
ALL_EMBEDDING_ENTITY_TYPES: tuple[EmbeddingEntityType, ...] = (
    "decision",
    "learning",
    "snippet",
    "runbook",
    "adr",
)


class EmbeddingBacklogRepository(Protocol):
    async def embedding_backlog_stats(
        self,
        *,
        project_key: str | None = None,
    ) -> Any: ...

    async def list_embedding_backlog(
        self,
        *,
        limit: int,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def set_embedding_if_current(
        self,
        entity_id: UUID,
        embedding: list[float],
        *,
        expected_updated_at: datetime,
    ) -> dict[str, Any] | None: ...

    async def get_by_id(self, entity_id: UUID) -> Any | None: ...


class BatchEmbeddingService(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class EntityBackfillReport:
    pending: int = 0
    attempted: int = 0
    stored: int = 0
    stale: int = 0
    missing: int = 0
    unavailable: int = 0
    unavailable_by_kind: dict[str, int] = field(default_factory=dict)
    timed_out: int = 0
    failed: int = 0
    skipped_empty: int = 0


@dataclass(slots=True)
class BackfillReport:
    by_entity_type: dict[EmbeddingEntityType, EntityBackfillReport]
    dry_run: bool
    lock_acquired: bool | None = None
    metrics_persisted: bool | None = None

    @property
    def has_failures(self) -> bool:
        return (
            any(
                item.unavailable > 0
                or item.timed_out > 0
                or item.failed > 0
                or item.skipped_empty > 0
                for item in self.by_entity_type.values()
            )
            or self.metrics_persisted is False
        )


SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


async def persist_backfill_metrics(
    session_factory: SessionFactory,
    report: BackfillReport,
) -> bool:
    """Accumulate one executed report in the existing seven-day metrics table."""
    values: dict[str, int] = {
        name: sum(getattr(item, name) for item in report.by_entity_type.values())
        for name in (
            "attempted",
            "stored",
            "stale",
            "missing",
            "unavailable",
            "timed_out",
            "failed",
        )
    }
    for item in report.by_entity_type.values():
        for kind, count in item.unavailable_by_kind.items():
            key = f"unavailable.{kind}"
            values[key] = values.get(key, 0) + count

    try:
        async with session_factory() as session:
            for name, value in values.items():
                if value == 0:
                    continue
                await session.execute(
                    sa.text(
                        "INSERT INTO metrics_timeseries (bucket_ts, metric, value) "
                        "VALUES (date_trunc('hour', NOW()), :metric, :value) "
                        "ON CONFLICT (bucket_ts, metric) DO UPDATE "
                        "SET value = metrics_timeseries.value + EXCLUDED.value"
                    ),
                    {"metric": f"embedding_backfill.{name}", "value": float(value)},
                )
            await session.commit()
    except Exception:  # noqa: BLE001 - metrics failure is reported, not destructive
        logger.exception("embedding_backfill.metrics_persist_failed")
        return False
    return True


class EmbeddingBackfillJob:
    """Embed bounded batches and store them with the shared compare-and-set."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        repos: Mapping[EmbeddingEntityType, EmbeddingBacklogRepository],
        embedding_svc: BatchEmbeddingService,
        feature_linker: Any | None = None,
        auto_linker: Any | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._session_factory = session_factory
        self._repos = dict(repos)
        self._embedding_svc = embedding_svc
        self._feature_linker = feature_linker
        self._auto_linker = auto_linker
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        *,
        entity_types: Sequence[EmbeddingEntityType] | None = None,
        batch_size: int = 20,
        limit: int = 200,
        project_key: str | None = None,
        dry_run: bool = False,
    ) -> BackfillReport:
        """Process at most ``limit`` pending rows per selected entity type."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if limit < 1:
            raise ValueError("limit must be positive")

        selected = tuple(entity_types or self._repos.keys())
        missing_repos = [entity_type for entity_type in selected if entity_type not in self._repos]
        if missing_repos:
            raise ValueError(f"Missing repositories for: {', '.join(missing_repos)}")
        report = BackfillReport(
            by_entity_type={entity_type: EntityBackfillReport() for entity_type in selected},
            dry_run=dry_run,
        )

        if dry_run:
            await self._process_selected(
                selected,
                report,
                batch_size=batch_size,
                limit=limit,
                project_key=project_key,
                dry_run=True,
            )
            return report

        async with self._session_factory() as lock_session:
            lock_result = await lock_session.execute(
                sa.select(sa.func.pg_try_advisory_lock(EMBEDDING_BACKFILL_LOCK))
            )
            if not bool(lock_result.scalar_one()):
                report.lock_acquired = False
                return report
            report.lock_acquired = True
            try:
                await self._process_selected(
                    selected,
                    report,
                    batch_size=batch_size,
                    limit=limit,
                    project_key=project_key,
                    dry_run=False,
                )
            finally:
                await lock_session.execute(
                    sa.select(sa.func.pg_advisory_unlock(EMBEDDING_BACKFILL_LOCK))
                )
        return report

    async def _process_selected(
        self,
        selected: Sequence[EmbeddingEntityType],
        report: BackfillReport,
        *,
        batch_size: int,
        limit: int,
        project_key: str | None,
        dry_run: bool,
    ) -> None:
        for entity_type in selected:
            repo = self._repos[entity_type]
            entity_report = report.by_entity_type[entity_type]
            if dry_run:
                stats = await repo.embedding_backlog_stats(project_key=project_key)
                entity_report.pending = stats.count
                continue
            rows = await repo.list_embedding_backlog(limit=limit, project_key=project_key)
            entity_report.pending = len(rows)
            for start in range(0, len(rows), batch_size):
                await self._process_batch(
                    entity_type,
                    repo,
                    rows[start : start + batch_size],
                    entity_report,
                )

    async def _process_batch(
        self,
        entity_type: EmbeddingEntityType,
        repo: EmbeddingBacklogRepository,
        rows: list[dict[str, Any]],
        report: EntityBackfillReport,
    ) -> None:
        eligible_rows: list[dict[str, Any]] = []
        texts: list[str] = []
        for row in rows:
            text = embedding_text_from_row(entity_type, row)
            if not text.strip():
                report.skipped_empty += 1
                continue
            eligible_rows.append(row)
            texts.append(text)
        if not eligible_rows:
            return

        report.attempted += len(eligible_rows)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                embeddings = await self._embedding_svc.embed_texts(texts)
        except TimeoutError:
            report.timed_out += len(eligible_rows)
            logger.warning(
                "embedding_backfill.timed_out",
                entity_type=entity_type,
                count=len(eligible_rows),
            )
            return
        except EmbeddingUnavailable as exc:
            report.unavailable += len(eligible_rows)
            report.unavailable_by_kind[exc.kind] = report.unavailable_by_kind.get(
                exc.kind, 0
            ) + len(eligible_rows)
            logger.warning(
                "embedding_backfill.unavailable",
                entity_type=entity_type,
                count=len(eligible_rows),
                kind=exc.kind,
            )
            return
        except Exception as exc:  # noqa: BLE001 - continue independent batches
            report.failed += len(eligible_rows)
            logger.error(
                "embedding_backfill.batch_failed",
                entity_type=entity_type,
                count=len(eligible_rows),
                reason=type(exc).__name__,
            )
            return

        if not isinstance(embeddings, list) or len(embeddings) != len(eligible_rows):
            report.failed += len(eligible_rows)
            logger.error(
                "embedding_backfill.cardinality_mismatch",
                entity_type=entity_type,
                expected=len(eligible_rows),
                observed=len(embeddings) if isinstance(embeddings, list) else None,
            )
            return

        for row, embedding in zip(eligible_rows, embeddings, strict=True):
            if (
                not isinstance(embedding, list)
                or len(embedding) != _EMBEDDING_DIM
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in embedding
                )
            ):
                report.failed += 1
                logger.error(
                    "embedding_backfill.invalid_embedding",
                    entity_type=entity_type,
                    count=1,
                    reason="non_comparable",
                )
                continue
            numeric_embedding = [float(value) for value in embedding]
            try:
                norm_sq = math.fsum(value * value for value in numeric_embedding)
            except OverflowError:
                norm_sq = math.inf
            if not math.isfinite(norm_sq) or norm_sq <= MIN_COMPARABLE_EMBEDDING_NORM**2:
                report.failed += 1
                logger.error(
                    "embedding_backfill.invalid_embedding",
                    entity_type=entity_type,
                    count=1,
                    reason="non_comparable",
                )
                continue
            await self._store_one(entity_type, repo, row, numeric_embedding, report)

    async def _store_one(
        self,
        entity_type: EmbeddingEntityType,
        repo: EmbeddingBacklogRepository,
        row: dict[str, Any],
        embedding: list[float],
        report: EntityBackfillReport,
    ) -> None:
        try:
            stored_row = await repo.set_embedding_if_current(
                row["id"],
                embedding,
                expected_updated_at=row["updated_at"],
            )
        except Exception as exc:  # noqa: BLE001 - continue independent rows
            report.failed += 1
            logger.error(
                "embedding_backfill.store_failed",
                entity_type=entity_type,
                entity_id=str(row["id"]),
                reason=type(exc).__name__,
            )
            return
        if stored_row is None:
            try:
                current = await repo.get_by_id(row["id"])
            except Exception as exc:  # noqa: BLE001
                report.failed += 1
                logger.error(
                    "embedding_backfill.classify_failed",
                    entity_type=entity_type,
                    entity_id=str(row["id"]),
                    reason=type(exc).__name__,
                )
                return
            if current is None:
                report.missing += 1
            else:
                report.stale += 1
            return

        report.stored += 1
        title = row.get("title") or row.get("topic")
        await link_artifact_if_enabled(
            self._feature_linker,
            embedding,
            entity_type,
            row["id"],
            row.get("project_key"),
            title,
        )
        graph_type = "ADR" if entity_type == "adr" else entity_type.title()
        # Result ignored on purpose (6d2cf2a9 d): this backfill already reports
        # its own count, it has no caller to surface per-entity detail to.
        _link_job = await auto_link_if_enabled(self._auto_linker, graph_type, row["id"], embedding)
