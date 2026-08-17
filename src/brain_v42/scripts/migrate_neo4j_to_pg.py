#!/usr/bin/env python3
"""migrate_neo4j_to_pg.py — One-shot migration from Neo4j (datalake_v2) to PostgreSQL (brain_v42).

Usage:
    python scripts/migrate_neo4j_to_pg.py \\
        --neo4j-password secret \\
        --postgres-url postgresql+asyncpg://brain:brain@localhost:5433/brain

    # Dry run (no DB writes):
    python scripts/migrate_neo4j_to_pg.py --neo4j-password secret --dry-run

    # Regenerate embeddings via ONNX EmbeddingService:
    python scripts/migrate_neo4j_to_pg.py --neo4j-password secret --regen-embeddings

    # Migrate only specific entity types:
    python scripts/migrate_neo4j_to_pg.py --neo4j-password secret \\
        --entity-types Decision,Learning

IMPORTANT: This script COPIES data. Neo4j is never modified.
Run AFTER applying Alembic migrations: alembic upgrade head
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

try:
    from neo4j import AsyncGraphDatabase

    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    AsyncGraphDatabase = None  # type: ignore[assignment,misc]

# ─── Logging setup ────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger("migrate_neo4j_to_pg")

# ─── Entity type definitions ──────────────────────────────────────────────────

ALL_ENTITY_TYPES = ["Decision", "Learning", "Snippet", "Runbook", "ADR", "ProjectContext"]

# Required fields per entity type (non-nullable, no server default)
REQUIRED_FIELDS: dict[str, list[str]] = {
    "Decision": ["title", "description", "reasoning"],
    "Learning": ["topic", "insight"],
    "Snippet": ["title", "intention", "code", "language"],
    "Runbook": ["title", "description", "project_key", "trigger"],
    "ADR": ["number", "title", "context", "decision", "consequences", "project_key"],
    "ProjectContext": ["project_key", "name", "description"],
}

# ─── Helper functions ──────────────────────────────────────────────────────────


def to_uuid(value: Any) -> uuid.UUID | None:
    """Convert a Neo4j string UUID or UUID object to Python uuid.UUID."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def to_datetime_utc(value: Any) -> datetime | None:
    """Convert Neo4j DateTime or string to timezone-aware UTC datetime."""
    if value is None:
        return None
    if hasattr(value, "to_native"):
        # `value: Any` (a duck-typed Neo4j DateTime, checked via hasattr,
        # not isinstance -- there's no import of neo4j.time here), so
        # `.to_native()`'s return type is Any regardless of what it holds.
        dt = value.to_native()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt  # type: ignore[no-any-return]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    return None


def to_list(value: Any, default: list[Any] | None = None) -> list[Any]:
    """Coerce a Neo4j value to a Python list."""
    if value is None:
        return default if default is not None else []
    if isinstance(value, list):
        return value
    return [value]


def to_dict(value: Any) -> dict[str, Any]:
    """Coerce a Neo4j value (map, string, or None) to a Python dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)  # type: ignore[no-any-return]
    return {}


def to_json_list(value: Any) -> list[Any]:
    """Coerce a Neo4j JSONB-stored list (list, JSON string, or None) to Python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def to_embedding(value: Any) -> list[float] | None:
    """Convert a Neo4j embedding property to list[float] or None."""
    if value is None:
        return None
    if isinstance(value, list):
        return [float(x) for x in value]
    return None


# ─── Node extraction helpers ───────────────────────────────────────────────────


def extract_decision(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce a Decision node from Neo4j to a PG-ready dict."""
    row: dict[str, Any] = {
        "title": node.get("title"),
        "description": node.get("description"),
        "reasoning": node.get("reasoning"),
        "alternatives": to_list(node.get("alternatives")),
        "consequences": node.get("consequences"),
        "project_key": node.get("project_key"),
        "status": node.get("status", "active"),
        "tags": to_list(node.get("tags")),
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    # UUID fields
    row["id"] = to_uuid(node.get("id"))
    superseded_by = to_uuid(node.get("superseded_by"))
    if superseded_by is not None:
        row["superseded_by"] = superseded_by
    # Embedding
    embedding = to_embedding(node.get("embedding"))
    if embedding is not None:
        row["embedding"] = embedding
    return row


def extract_learning(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce a Learning node from Neo4j to a PG-ready dict."""
    row: dict[str, Any] = {
        "topic": node.get("topic"),
        "insight": node.get("insight"),
        "source": node.get("source"),
        "source_type": node.get("source_type", "experience"),
        "confidence": node.get("confidence", "medium"),
        "project_key": node.get("project_key"),
        "tags": to_list(node.get("tags")),
        "validated_at": to_datetime_utc(node.get("validated_at")),
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    row["id"] = to_uuid(node.get("id"))
    embedding = to_embedding(node.get("embedding"))
    if embedding is not None:
        row["embedding"] = embedding
    return row


def extract_snippet(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce a Snippet node from Neo4j to a PG-ready dict."""
    row: dict[str, Any] = {
        "title": node.get("title"),
        "intention": node.get("intention"),
        "code": node.get("code"),
        "language": node.get("language"),
        "dependencies": to_list(node.get("dependencies")),
        "usage_example": node.get("usage_example"),
        "gotchas": node.get("gotchas"),
        "project_key": node.get("project_key"),
        "tags": to_list(node.get("tags")),
        "use_count": node.get("use_count", 0),
        "last_used_at": to_datetime_utc(node.get("last_used_at")),
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    row["id"] = to_uuid(node.get("id"))
    embedding = to_embedding(node.get("embedding"))
    if embedding is not None:
        row["embedding"] = embedding
    return row


def extract_runbook(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce a Runbook node from Neo4j to a PG-ready dict."""
    row: dict[str, Any] = {
        "title": node.get("title"),
        "description": node.get("description"),
        "project_key": node.get("project_key"),
        "trigger": node.get("trigger"),
        "prerequisites": to_list(node.get("prerequisites")),
        "steps": to_json_list(node.get("steps")),
        "rollback_steps": to_json_list(node.get("rollback_steps")),
        "estimated_duration": node.get("estimated_duration"),
        "tags": to_list(node.get("tags")),
        "execution_count": node.get("execution_count", 0),
        "last_executed_at": to_datetime_utc(node.get("last_executed_at")),
        "last_execution_status": node.get("last_execution_status"),
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    row["id"] = to_uuid(node.get("id"))
    embedding = to_embedding(node.get("embedding"))
    if embedding is not None:
        row["embedding"] = embedding
    return row


def extract_adr(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce an ADR node from Neo4j to a PG-ready dict."""
    row: dict[str, Any] = {
        "number": node.get("number"),
        "title": node.get("title"),
        "context": node.get("context"),
        "decision": node.get("decision"),
        "consequences": node.get("consequences"),
        "alternatives_considered": to_json_list(node.get("alternatives_considered")),
        "project_key": node.get("project_key"),
        "tags": to_list(node.get("tags")),
        "status": node.get("status", "proposed"),
        "decided_at": to_datetime_utc(node.get("decided_at")),
        "superseded_by": node.get("superseded_by"),  # int or None
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    row["id"] = to_uuid(node.get("id"))
    embedding = to_embedding(node.get("embedding"))
    if embedding is not None:
        row["embedding"] = embedding
    return row


def extract_project_context(node: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and coerce a ProjectContext node from Neo4j to a PG-ready dict.

    Note: ProjectContext has no embedding or search_vector columns in PG.
    """
    row: dict[str, Any] = {
        "project_key": node.get("project_key"),
        "name": node.get("name"),
        "description": node.get("description"),
        "languages": to_list(node.get("languages")),
        "frameworks": to_list(node.get("frameworks")),
        "databases": to_list(node.get("databases")),
        "code_style": node.get("code_style"),
        "git_workflow": node.get("git_workflow"),
        "test_strategy": node.get("test_strategy"),
        "current_phase": node.get("current_phase"),
        "current_focus": node.get("current_focus"),
        "blockers": to_list(node.get("blockers")),
        "related_projects": to_list(node.get("related_projects")),
        "local_path": node.get("local_path"),
        "repo_url": node.get("repo_url"),
        "metadata": to_dict(node.get("metadata")),
        "created_at": to_datetime_utc(node.get("created_at")),
        "updated_at": to_datetime_utc(node.get("updated_at")),
    }
    row["id"] = to_uuid(node.get("id"))
    return row


EXTRACTORS = {
    "Decision": extract_decision,
    "Learning": extract_learning,
    "Snippet": extract_snippet,
    "Runbook": extract_runbook,
    "ADR": extract_adr,
    "ProjectContext": extract_project_context,
}

# Map entity type → PG table name
TABLE_NAMES = {
    "Decision": "decisions",
    "Learning": "learnings",
    "Snippet": "snippets",
    "Runbook": "runbooks",
    "ADR": "adrs",
    "ProjectContext": "project_contexts",
}

# Embedding text builder per entity type (for --regen-embeddings)
# ProjectContext has no embedding column — excluded here.
EMBEDDING_TEXT_BUILDERS: dict[str, Any] = {
    "Decision": lambda r: (
        f"{r.get('title', '')} {r.get('description', '')} {r.get('reasoning', '')}"
    ),
    "Learning": lambda r: f"{r.get('topic', '')} {r.get('insight', '')}",
    "Snippet": lambda r: f"{r.get('title', '')} {r.get('intention', '')} {r.get('code', '')}",
    "Runbook": lambda r: f"{r.get('title', '')} {r.get('description', '')} {r.get('trigger', '')}",
    "ADR": lambda r: f"{r.get('title', '')} {r.get('context', '')} {r.get('decision', '')}",
}


# ─── Validation ────────────────────────────────────────────────────────────────


def validate_required_fields(entity_type: str, row: dict[str, Any]) -> list[str]:
    """Return list of missing required field names."""
    missing = []
    for field in REQUIRED_FIELDS.get(entity_type, []):
        if row.get(field) is None:
            missing.append(field)
    return missing


# ─── Dry-run display ───────────────────────────────────────────────────────────


def print_dry_run_summary(
    entity_type: str, records: list[dict[str, Any]], sample_size: int = 3
) -> None:
    """Print a dry-run summary for one entity type."""
    count = len(records)
    logger.info("dry_run.entity_summary", entity_type=entity_type, count=count)
    if records:
        # Show field names from the first record as a sample
        sample_fields = list(records[0].keys())[:sample_size]
        logger.info(
            "dry_run.sample_fields",
            entity_type=entity_type,
            sample_fields=sample_fields,
        )


# ─── Core migration logic ──────────────────────────────────────────────────────


async def fetch_neo4j_records(
    neo4j_driver: Any,
    entity_type: str,
) -> list[dict[str, Any]]:
    """Fetch all nodes of entity_type from Neo4j and return as list of dicts."""
    async with neo4j_driver.session() as session:
        result = await session.run(f"MATCH (n:{entity_type}) RETURN n")
        records = []
        async for record in result:
            node = dict(record["n"])
            records.append(node)
    return records


async def count_pg_rows(session_factory: Any, table_name: str) -> int:
    """Count existing rows in a PG table."""
    from sqlalchemy import text as sa_text

    async with session_factory() as session:
        result = await session.execute(
            sa_text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608  # nosec B608 # every call site (line 693) passes TABLE_NAMES[entity_type] (line 312), a module dict literal, no caller input (audited 2026-08-17)
        )
        return int(result.scalar() or 0)


async def insert_entity_batch(
    session_factory: Any,
    entity_type: str,
    rows: list[dict[str, Any]],
    regen_embeddings: bool,
    embedding_svc: Any,
    batch_size: int,
) -> tuple[int, int, int]:
    """Insert a list of extracted rows into PostgreSQL using ON CONFLICT DO NOTHING.

    Returns:
        Tuple of (inserted_count, skipped_count, error_count).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from brain_v42.db.tables import (
        adrs,
        decisions,
        learnings,
        project_contexts,
        runbooks,
        snippets,
    )

    table_map = {
        "Decision": decisions,
        "Learning": learnings,
        "Snippet": snippets,
        "Runbook": runbooks,
        "ADR": adrs,
        "ProjectContext": project_contexts,
    }
    table = table_map[entity_type]

    inserted = 0
    skipped = 0
    errors = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for row in batch:
            try:
                # Optionally regenerate embedding via EmbeddingService
                if regen_embeddings and entity_type in EMBEDDING_TEXT_BUILDERS:
                    builder = EMBEDDING_TEXT_BUILDERS[entity_type]
                    text_for_embedding = builder(row)
                    # embed_text is synchronous (ONNX Runtime)
                    row["embedding"] = embedding_svc.embed_text(text_for_embedding)

                # Remove None values for columns with server defaults
                # (id can be None only if not present in Neo4j, which is unlikely)
                clean_row = {k: v for k, v in row.items() if v is not None}

                stmt = (
                    pg_insert(table)
                    .values(**clean_row)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                async with session_factory() as session:
                    async with session.begin():
                        result = await session.execute(stmt)
                if result.rowcount == 0:
                    skipped += 1
                    logger.debug(
                        "insert.conflict_skipped",
                        entity_type=entity_type,
                        id=str(row.get("id")),
                    )
                else:
                    inserted += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    "insert.error",
                    entity_type=entity_type,
                    id=str(row.get("id")),
                    error=str(exc),
                )

    return inserted, skipped, errors


# ─── Main entry point ──────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate data from Neo4j (datalake_v2) to PostgreSQL (brain_v42).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--neo4j-uri",
        default="bolt://localhost:7688",
        help="Neo4j bolt URI (default: bolt://localhost:7688)",
    )
    parser.add_argument(
        "--neo4j-user",
        default="neo4j",
        help="Neo4j username (default: neo4j)",
    )
    parser.add_argument(
        "--neo4j-password",
        required=True,
        help="Neo4j password (required)",
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help=(
            "PostgreSQL async URL (default: reads POSTGRES_URL env var). "
            "Format: postgresql+asyncpg://user:pass@host:port/db"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated; do NOT write to PostgreSQL.",
    )
    parser.add_argument(
        "--regen-embeddings",
        action="store_true",
        help="Regenerate embeddings via EmbeddingService (instead of copying from Neo4j).",
    )
    parser.add_argument(
        "--entity-types",
        default=",".join(ALL_ENTITY_TYPES),
        help=(
            f"Comma-separated list of entity types to migrate "
            f"(default: all — {','.join(ALL_ENTITY_TYPES)})"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of records per PG insert batch (default: 50).",
    )
    return parser.parse_args()


async def main() -> int:
    """Main async entry point. Returns exit code (0=success, 1=error)."""
    args = parse_args()

    # Validate neo4j driver availability
    if not NEO4J_AVAILABLE:
        logger.error(
            "neo4j_driver_missing",
            message=(
                "The 'neo4j' Python driver is not installed. "
                "Install it with: pip install neo4j>=5.0"
            ),
        )
        return 1

    # Parse entity types
    entity_types = [e.strip() for e in args.entity_types.split(",") if e.strip()]
    invalid = [e for e in entity_types if e not in ALL_ENTITY_TYPES]
    if invalid:
        logger.error(
            "invalid_entity_types",
            invalid=invalid,
            valid=ALL_ENTITY_TYPES,
        )
        return 1

    logger.info(
        "migration.start",
        entity_types=entity_types,
        dry_run=args.dry_run,
        regen_embeddings=args.regen_embeddings,
        batch_size=args.batch_size,
    )

    # ── Step 1: Connect to Neo4j ───────────────────────────────────────────────
    neo4j_driver = AsyncGraphDatabase.driver(
        args.neo4j_uri,
        auth=(args.neo4j_user, args.neo4j_password),
    )

    try:
        # ── Step 2: Fetch all records from Neo4j ──────────────────────────────
        neo4j_counts: dict[str, int] = {}
        extracted_rows: dict[str, list[dict[str, Any]]] = {}

        for entity_type in entity_types:
            logger.info("neo4j.fetching", entity_type=entity_type)
            raw_nodes = await fetch_neo4j_records(neo4j_driver, entity_type)
            neo4j_counts[entity_type] = len(raw_nodes)
            logger.info("neo4j.fetched", entity_type=entity_type, count=neo4j_counts[entity_type])

            # Extract + coerce each node
            extractor = EXTRACTORS[entity_type]
            valid_rows: list[dict[str, Any]] = []
            for raw_node in raw_nodes:
                try:
                    row = extractor(raw_node)
                    if row is None:
                        # Extractor already decided this node is unusable
                        # (e.g. missing a base field it needs before the
                        # generic required-fields check even applies).
                        logger.warning(
                            "extract.extractor_returned_none",
                            entity_type=entity_type,
                            id=str(raw_node.get("id")),
                        )
                        continue
                    missing = validate_required_fields(entity_type, row)
                    if missing:
                        logger.warning(
                            "extract.missing_required_fields",
                            entity_type=entity_type,
                            id=str(raw_node.get("id")),
                            missing_fields=missing,
                        )
                        continue
                    valid_rows.append(row)
                except Exception as exc:
                    logger.warning(
                        "extract.error",
                        entity_type=entity_type,
                        id=str(raw_node.get("id")),
                        error=str(exc),
                    )
                    continue
            extracted_rows[entity_type] = valid_rows

            if args.dry_run:
                print_dry_run_summary(entity_type, valid_rows)

        # ── Step 3: Dry-run exits here ─────────────────────────────────────────
        if args.dry_run:
            logger.info("dry_run.complete", message="No data was written to PostgreSQL.")
            print("\n--- Dry-run Summary ---")
            print(f"{'Entity':<20} {'Neo4j Count':<15} {'Valid Rows':<15}")
            print("-" * 50)
            for entity_type in entity_types:
                table_name = TABLE_NAMES[entity_type]
                _ = table_name  # table name not queried in dry-run
                print(
                    f"{entity_type:<20} {neo4j_counts[entity_type]:<15} "
                    f"{len(extracted_rows[entity_type]):<15}"
                )
            return 0

        # ── Step 4: Set up PostgreSQL connection ──────────────────────────────
        postgres_url = args.postgres_url or os.environ.get("POSTGRES_URL")
        if not postgres_url:
            logger.error(
                "postgres_url_missing",
                message=(
                    "PostgreSQL URL not provided. Use --postgres-url or set POSTGRES_URL env var."
                ),
            )
            return 1

        # Set env var before importing brain_v42.db (engine reads settings via env)
        os.environ["POSTGRES_URL"] = postgres_url

        from brain_v42.db.engine import get_session_factory

        session_factory = get_session_factory()

        # ── Step 5: Set up EmbeddingService if needed ─────────────────────────
        embedding_svc = None
        if args.regen_embeddings:
            from brain_v42.services.embedding_service import EmbeddingService

            embedding_svc = EmbeddingService()
            logger.info("embedding_service.initialized")

        # ── Step 6: Insert rows into PostgreSQL ───────────────────────────────
        summary: dict[str, dict[str, int]] = {}

        for entity_type in entity_types:
            rows = extracted_rows[entity_type]
            logger.info(
                "pg.inserting",
                entity_type=entity_type,
                row_count=len(rows),
            )

            inserted, skipped, errors = await insert_entity_batch(
                session_factory=session_factory,
                entity_type=entity_type,
                rows=rows,
                regen_embeddings=args.regen_embeddings,
                embedding_svc=embedding_svc,
                batch_size=args.batch_size,
            )

            logger.info(
                "pg.insert_done",
                entity_type=entity_type,
                inserted=inserted,
                skipped=skipped,
                errors=errors,
            )

            # Count final PG rows for summary
            pg_count = await count_pg_rows(session_factory, TABLE_NAMES[entity_type])

            summary[entity_type] = {
                "neo4j": neo4j_counts[entity_type],
                "pg_after": pg_count,
                "inserted": inserted,
                "skipped": skipped,
                "errors": errors,
            }

        # ── Step 7: Print summary table ───────────────────────────────────────
        print("\n--- Migration Summary ---")
        header = f"{'Entity':<20} {'Neo4j':<10} {'PG':<10} {'Inserted':<12} {'Skipped':<12} {'Errors':<8}"
        print(header)
        print("-" * len(header))

        total_inserted = 0
        total_skipped = 0
        total_errors = 0
        for entity_type in entity_types:
            s = summary[entity_type]
            print(
                f"{entity_type:<20} {s['neo4j']:<10} {s['pg_after']:<10} "
                f"{s['inserted']:<12} {s['skipped']:<12} {s['errors']:<8}"
            )
            total_inserted += s["inserted"]
            total_skipped += s["skipped"]
            total_errors += s["errors"]

        print("-" * len(header))
        print(
            f"{'TOTAL':<20} {'':<10} {'':<10} "
            f"{total_inserted:<12} {total_skipped:<12} {total_errors:<8}"
        )

        if total_errors > 0:
            logger.warning(
                "migration.completed_with_errors",
                total_inserted=total_inserted,
                total_skipped=total_skipped,
                total_errors=total_errors,
            )
            return 1

        logger.info(
            "migration.completed",
            total_inserted=total_inserted,
            total_skipped=total_skipped,
        )
        return 0

    finally:
        await neo4j_driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
