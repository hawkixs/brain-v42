#!/usr/bin/env python3
"""Refresh stored plan vectors without scanning source files or changing plan metadata.

The default command only inventories the selected parent/chunk cohort.  A write
needs every ``--apply`` precondition, an exclusive private recovery path, and
the expected database and embedding-model identities.  Provider calls happen
outside the short locked transaction; the transaction refuses any cohort or
source drift before it updates vector columns only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from brain_v42.maintenance.plan_index_repair import RepairSafetyError, write_private_json
from brain_v42.services.embedding_factory import (
    build_embedding_service,
    settings_for_standalone_script,
)
from brain_v42.services.embedding_text import reproducible_embedding_text

logger = structlog.get_logger(__name__)

_LOCK_TIMEOUT = "5s"
_PARENT_COLUMNS = """
id::text AS id, file_path, title, plan_type, project_key, content_hash,
embedding::text AS embedding, content, summary, search_vector::text AS search_vector,
tags, metadata, status, chunk_count, word_count, access_count, access_count_human,
freshness_status_updated_at, freshness_source, last_accessed_at_human,
last_accessed_at, freshness_status, indexed_at, created_at, updated_at
"""
_CHUNK_COLUMNS = """
id::text AS id, plan_id::text AS plan_id, section_title, section_path, content,
section_order, word_count, embedding::text AS embedding, search_vector::text AS search_vector,
tags, project_key, plan_type, status, access_count, last_accessed_at, created_at
"""


class RefreshRefusal(RuntimeError):
    """A safe, stable refusal reason for the operational boundary."""


@dataclass(frozen=True, slots=True)
class PlanVectorSnapshot:
    """The complete selected source and vector state, ordered for exact CAS."""

    database: str
    model: str
    project_key: str | None
    parents: tuple[dict[str, object], ...]
    chunks: tuple[dict[str, object], ...]

    def recovery_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "database": self.database,
            "model": self.model,
            "project_key": self.project_key,
            "parents": list(self.parents),
            "chunks": list(self.chunks),
        }


def _json_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _record(row: asyncpg.Record) -> dict[str, object]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _pgvector(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"


def validate_vectors(
    vectors: Sequence[Sequence[float]], *, expected_count: int, dimension: int
) -> list[list[float]]:
    """Refuse provider output that cannot safely inhabit the vector columns."""
    if not isinstance(vectors, Sequence) or isinstance(vectors, (str, bytes)):
        raise RefreshRefusal("embedding_cardinality")
    if len(vectors) != expected_count:
        raise RefreshRefusal("embedding_cardinality")
    checked: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            raise RefreshRefusal("embedding_invalid")
        if len(vector) != dimension:
            raise RefreshRefusal("embedding_dimension")
        try:
            normalized = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise RefreshRefusal("embedding_invalid") from exc
        if not all(math.isfinite(value) for value in normalized) or not any(normalized):
            raise RefreshRefusal("embedding_invalid")
        checked.append(normalized)
    return checked


def _validate_recovery_path(path: Path) -> None:
    """Reject paths that could redirect the private recovery artifact."""
    if not path.is_absolute():
        raise RefreshRefusal("recovery_path_not_absolute")
    if path.exists() or path.is_symlink():
        raise RefreshRefusal("recovery_file_exists")
    current = path.parent
    while True:
        try:
            details = os.lstat(current)
        except OSError as exc:
            raise RefreshRefusal("recovery_parent_invalid") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise RefreshRefusal("recovery_parent_invalid")
        if current.parent == current:
            break
        current = current.parent


def write_recovery_snapshot(path: Path, snapshot: PlanVectorSnapshot) -> str:
    """Persist the pre-write state privately, before any vector mutation."""
    _validate_recovery_path(path)
    try:
        return str(write_private_json(path, snapshot.recovery_payload(), no_follow_parents=True))
    except RepairSafetyError as exc:
        raise RefreshRefusal(exc.reason_code) from exc


async def read_snapshot(
    conn: asyncpg.Connection,
    *,
    database: str,
    model: str,
    project_key: str | None,
    lock: bool = False,
) -> PlanVectorSnapshot:
    """Read parents and every belonging chunk as one ordered cohort."""
    parent_filter = "" if project_key is None else " WHERE project_key = $1"
    lock_suffix = " FOR UPDATE" if lock else ""
    parent_rows = await conn.fetch(
        f"SELECT {_PARENT_COLUMNS} FROM indexed_plans{parent_filter} ORDER BY id{lock_suffix}",
        *(() if project_key is None else (project_key,)),
    )
    parents = tuple(_record(row) for row in parent_rows)
    parent_ids = [row["id"] for row in parents]
    if parent_ids:
        chunk_rows = await conn.fetch(
            f"SELECT {_CHUNK_COLUMNS} FROM indexed_plan_chunks "
            f"WHERE plan_id = ANY($1::uuid[]) ORDER BY plan_id, id{lock_suffix}",
            parent_ids,
        )
    else:
        chunk_rows = []
    chunks = tuple(_record(row) for row in chunk_rows)
    if any(
        chunk["project_key"]
        != next(parent["project_key"] for parent in parents if parent["id"] == chunk["plan_id"])
        for chunk in chunks
    ):
        raise RefreshRefusal("project_cohort_mismatch")
    return PlanVectorSnapshot(database, model, project_key, parents, chunks)


async def read_consistent_snapshot(
    conn: asyncpg.Connection,
    *,
    database: str,
    model: str,
    project_key: str | None,
) -> PlanVectorSnapshot:
    """Freeze the parent/chunk read at one database snapshot before provider I/O."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        return await read_snapshot(
            conn,
            database=database,
            model=model,
            project_key=project_key,
        )


def validate_cohort(snapshot: PlanVectorSnapshot, *, plans: int, chunks: int) -> None:
    """Counts are operator-approved inputs, never copied from a past inventory."""
    if len(snapshot.parents) != plans or len(snapshot.chunks) != chunks:
        raise RefreshRefusal("expected_cohort_mismatch")
    if not snapshot.parents:
        raise RefreshRefusal("empty_cohort")


def compose_inputs(snapshot: PlanVectorSnapshot) -> list[tuple[str, str, str]]:
    """Use the same canonical composers as the indexer and drift checker."""
    inputs: list[tuple[str, str, str]] = []
    for parent in snapshot.parents:
        text = reproducible_embedding_text("plan", parent)
        if not text or not text.strip():
            raise RefreshRefusal("parent_embedding_text_unavailable")
        inputs.append(("parent", str(parent["id"]), text))
    for chunk in snapshot.chunks:
        text = reproducible_embedding_text("plan_chunk", chunk)
        if not text or not text.strip():
            raise RefreshRefusal("chunk_embedding_text_unavailable")
        inputs.append(("chunk", str(chunk["id"]), text))
    return inputs


async def embed_inputs(
    embedding_svc: Any, inputs: list[tuple[str, str, str]], *, batch_size: int, dimension: int
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Embed outside database locks, in the provider's bounded request batches."""
    parent_vectors: dict[str, list[float]] = {}
    chunk_vectors: dict[str, list[float]] = {}
    for start in range(0, len(inputs), batch_size):
        batch = inputs[start : start + batch_size]
        try:
            raw = await embedding_svc.embed_texts([item[2] for item in batch])
        except Exception as exc:
            raise RefreshRefusal("embedding_provider_failed") from exc
        vectors = validate_vectors(raw, expected_count=len(batch), dimension=dimension)
        for (kind, row_id, _text), vector in zip(batch, vectors, strict=True):
            (parent_vectors if kind == "parent" else chunk_vectors)[row_id] = vector
    return parent_vectors, chunk_vectors


async def apply_refresh(
    conn: asyncpg.Connection,
    snapshot: PlanVectorSnapshot,
    *,
    recovery_file: Path,
    parent_vectors: dict[str, list[float]],
    chunk_vectors: dict[str, list[float]],
) -> tuple[int, int, str]:
    """Lock, revalidate the original snapshot, save recovery, then update vectors only."""
    async with conn.transaction():
        await conn.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
        await conn.execute(
            "LOCK TABLE indexed_plans, indexed_plan_chunks IN SHARE ROW EXCLUSIVE MODE"
        )
        current = await read_snapshot(
            conn,
            database=snapshot.database,
            model=snapshot.model,
            project_key=snapshot.project_key,
            lock=True,
        )
        if current != snapshot:
            raise RefreshRefusal("cohort_state_changed")
        if set(parent_vectors) != {str(row["id"]) for row in snapshot.parents}:
            raise RefreshRefusal("parent_vector_cohort_mismatch")
        if set(chunk_vectors) != {str(row["id"]) for row in snapshot.chunks}:
            raise RefreshRefusal("chunk_vector_cohort_mismatch")
        digest = write_recovery_snapshot(recovery_file, snapshot)
        for row_id, vector in parent_vectors.items():
            await conn.execute(
                "UPDATE indexed_plans SET embedding = $2::vector WHERE id = $1::uuid",
                row_id,
                _pgvector(vector),
            )
        for row_id, vector in chunk_vectors.items():
            await conn.execute(
                "UPDATE indexed_plan_chunks SET embedding = $2::vector WHERE id = $1::uuid",
                row_id,
                _pgvector(vector),
            )
    return len(parent_vectors), len(chunk_vectors), digest


def _require_apply_args(args: argparse.Namespace) -> None:
    required = (
        args.recovery_file,
        args.expected_plans,
        args.expected_chunks,
        args.expected_database,
        args.expected_model,
    )
    if not args.apply:
        return
    if any(value is None for value in required):
        raise RefreshRefusal("apply_preconditions_missing")
    if args.expected_plans < 0 or args.expected_chunks < 0:
        raise RefreshRefusal("expected_count_invalid")
    if not 1 <= args.batch_size <= 100:
        raise RefreshRefusal("batch_size_invalid")


async def run(args: argparse.Namespace) -> int:
    """Run the inventory or the explicitly armed refresh and expose no secrets."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        _require_apply_args(args)
        settings = settings_for_standalone_script(
            args.postgres_url or "postgresql://brain@localhost:5433/brain"
        )
        dsn = (args.postgres_url or settings.postgres_url).replace(
            "postgresql+asyncpg://", "postgresql://", 1
        )
        conn = await asyncpg.connect(dsn)
    except RefreshRefusal as exc:
        print(f"plan vector refresh refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("plan vector refresh refused", file=sys.stderr)
        return 2

    embedding_svc: Any | None = None
    exit_code = 2
    try:
        database = await conn.fetchval("SELECT current_database()")
        if not isinstance(database, str):
            raise RefreshRefusal("database_identity_unavailable")
        snapshot = await read_consistent_snapshot(
            conn, database=database, model=settings.embedding_model, project_key=args.project
        )
        if not args.apply:
            print(json.dumps({"plans": len(snapshot.parents), "chunks": len(snapshot.chunks)}))
            exit_code = 0
        else:
            if (
                database != args.expected_database
                or settings.embedding_model != args.expected_model
            ):
                raise RefreshRefusal("configured_identity_mismatch")
            validate_cohort(snapshot, plans=args.expected_plans, chunks=args.expected_chunks)
            _validate_recovery_path(Path(args.recovery_file))
            inputs = compose_inputs(snapshot)
            embedding_svc = build_embedding_service(settings)
            parents, chunks = await embed_inputs(
                embedding_svc,
                inputs,
                batch_size=args.batch_size,
                dimension=settings.embedding_dimension,
            )
            parent_count, chunk_count, _digest = await apply_refresh(
                conn,
                snapshot,
                recovery_file=Path(args.recovery_file),
                parent_vectors=parents,
                chunk_vectors=chunks,
            )
            print(
                json.dumps(
                    {
                        "plans_updated": parent_count,
                        "chunks_updated": chunk_count,
                        "database": database,
                        "model": settings.embedding_model,
                    }
                )
            )
            exit_code = 0
    except RefreshRefusal as exc:
        print(f"plan vector refresh refused: {exc}", file=sys.stderr)
        exit_code = 2
    except Exception:
        print("plan vector refresh refused", file=sys.stderr)
        exit_code = 2
    finally:
        try:
            if embedding_svc is not None:
                await embedding_svc.close()
        except Exception:
            print("plan vector refresh cleanup failed", file=sys.stderr)
            exit_code = 2
        finally:
            try:
                await conn.close()
            except Exception:
                print("plan vector refresh cleanup failed", file=sys.stderr)
                exit_code = 2
    return exit_code


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-url", help="Explicit database URL; otherwise use the configured environment"
    )
    parser.add_argument("--project")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recovery-file")
    parser.add_argument("--expected-plans", type=int)
    parser.add_argument("--expected-chunks", type=int)
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-model")
    parser.add_argument("--batch-size", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
