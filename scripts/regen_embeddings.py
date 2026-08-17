#!/usr/bin/env python3
"""regen_embeddings.py — Regenerate all embeddings via the GPU embedding service.

Reads rows from brain_v42 PostgreSQL tables, sends text batches to the GPU
embedding service (POST /embed), and writes the resulting vectors back.

Usage:
    python scripts/regen_embeddings.py                          # All tables
    python scripts/regen_embeddings.py --dry-run                # Count only
    python scripts/regen_embeddings.py --entity-types decisions  # Single table
    python scripts/regen_embeddings.py --batch-size 20          # Batch size

Environment variables:
    POSTGRES_URL              PostgreSQL connection URL (default: postgresql://brain:brain@localhost:5433/brain)
    EMBEDDING_SERVICE_URL     GPU embedding service URL (default: http://localhost:8003)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import time
from typing import Any

import asyncpg
import httpx

# ─── Entity type definitions ──────────────────────────────────────────────────

# Tables with embedding columns (project_contexts has no embedding)
ENTITY_TYPES = ["decisions", "learnings", "snippets", "runbooks", "adrs"]

# Text fields to compose for embedding, per entity type.
# The SQL query selects these columns and they are concatenated with spaces.
TEXT_FIELDS: dict[str, list[str]] = {
    "decisions": ["title", "description", "reasoning"],
    "learnings": ["topic", "insight"],
    "snippets": ["title", "intention"],
    "runbooks": ["title", "description", "trigger"],
    "adrs": ["title", "context", "decision"],
}


def _bounded_batch_size(value: str) -> int:
    batch_size = int(value)
    if not 1 <= batch_size <= 100:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 100")
    return batch_size


def compose_text(entity_type: str, row: asyncpg.Record) -> str:
    """Compose the text to embed from a database row."""
    fields = TEXT_FIELDS[entity_type]
    parts = [str(row[f] or "") for f in fields]
    return " ".join(parts).strip()


# ─── Database helpers ──────────────────────────────────────────────────────────


def clean_postgres_url(url: str) -> str:
    """Strip +asyncpg from SQLAlchemy-style URL for raw asyncpg usage.

    asyncpg expects postgresql:// not postgresql+asyncpg://.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def fetch_rows(
    pool: asyncpg.Pool,
    entity_type: str,
    only_missing: bool = False,
    project_key: str | None = None,
    since: dt.date | None = None,
) -> list[asyncpg.Record]:
    """Fetch rows from a table with optional filters.

    Args:
        only_missing: if True, restrict to rows where embedding IS NULL.
        project_key: if set, restrict to rows with this project_key.
        since: if set, restrict to rows with created_at >= since.
            Surgical for backfill of a known failure window.
    """
    fields = TEXT_FIELDS[entity_type]
    columns = ", ".join(["id"] + fields)
    query = f"SELECT {columns} FROM {entity_type}"  # noqa: S608
    clauses: list[str] = []
    params: list[Any] = []
    if only_missing:
        clauses.append("embedding IS NULL")
    if project_key is not None:
        params.append(project_key)
        clauses.append(f"project_key = ${len(params)}")
    if since is not None:
        params.append(since)
        clauses.append(f"created_at >= ${len(params)}")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at"
    return await pool.fetch(query, *params)


async def update_embeddings(
    pool: asyncpg.Pool,
    entity_type: str,
    ids: list[Any],
    embeddings: list[list[float]],
) -> int:
    """Update embedding column for a batch of rows. Returns count updated."""
    query = f"UPDATE {entity_type} SET embedding = $2, updated_at = NOW() WHERE id = $1"  # noqa: S608
    updated = 0
    async with pool.acquire() as conn:
        for row_id, embedding in zip(ids, embeddings, strict=True):
            # pgvector expects a string like '[0.1,0.2,...]' for vector type
            vector_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await conn.execute(query, row_id, vector_str)
            updated += 1
    return updated


# ─── GPU embedding service ─────────────────────────────────────────────────────


async def embed_batch(
    client: httpx.AsyncClient,
    service_url: str,
    texts: list[str],
) -> list[list[float]]:
    """Send a batch of texts to the GPU embedding service and return vectors."""
    response = await client.post(
        f"{service_url}/embed",
        json={"texts": texts},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


# ─── Main logic ───────────────────────────────────────────────────────────────


async def process_entity_type(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    service_url: str,
    entity_type: str,
    batch_size: int,
    dry_run: bool,
    only_missing: bool,
    project_key: str | None = None,
    since: dt.date | None = None,
) -> dict[str, int]:
    """Process a single entity type: fetch, embed, update.

    Returns a summary dict with keys: total, success, errors, skipped.
    """
    rows = await fetch_rows(
        pool,
        entity_type,
        only_missing=only_missing,
        project_key=project_key,
        since=since,
    )
    total = len(rows)

    if dry_run:
        print(f"  {entity_type}: {total} rows would be processed")
        return {"total": total, "success": 0, "errors": 0, "skipped": 0}

    if total == 0:
        print(f"  {entity_type}: 0 rows to process")
        return {"total": 0, "success": 0, "errors": 0, "skipped": 0}

    print(f"  {entity_type}: {total} rows to process")

    success = 0
    errors = 0
    skipped = 0

    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total + batch_size - 1) // batch_size

        # Compose texts for embedding
        texts = []
        valid_rows = []
        for row in batch:
            text = compose_text(entity_type, row)
            if not text:
                skipped += 1
                continue
            texts.append(text)
            valid_rows.append(row)

        if not texts:
            continue

        try:
            embeddings = await embed_batch(client, service_url, texts)
            ids = [row["id"] for row in valid_rows]
            updated = await update_embeddings(pool, entity_type, ids, embeddings)
            success += updated
            print(f"    batch {batch_num}/{total_batches}: {updated}/{len(texts)} updated")
        except httpx.HTTPStatusError as exc:
            errors += len(texts)
            print(
                f"    batch {batch_num}/{total_batches}: "
                f"HTTP error {exc.response.status_code} — {exc.response.text[:200]}"
            )
        except httpx.RequestError as exc:
            errors += len(texts)
            print(f"    batch {batch_num}/{total_batches}: request error — {exc}")
        except Exception as exc:
            errors += len(texts)
            print(f"    batch {batch_num}/{total_batches}: error — {exc}")

    return {"total": total, "success": success, "errors": errors, "skipped": skipped}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Regenerate embeddings for brain_v42 entities via GPU service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows only; do NOT call embedding service or update DB.",
    )
    parser.add_argument(
        "--entity-types",
        default=None,
        help=(
            f"Comma-separated list of entity types to process "
            f"(default: all — {','.join(ENTITY_TYPES)})"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_batch_size,
        default=20,
        help="Number of texts per embedding batch, from 1 to 100 (default: 20).",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only process rows where embedding IS NULL.",
    )
    parser.add_argument(
        "--project-key",
        default=None,
        help="Restrict to rows with this project_key (e.g. brain-v42).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Restrict to rows with created_at >= this ISO date "
            "(e.g. 2026-04-16). Use with --only-missing for surgical "
            "backfill of a known failure window."
        ),
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help=(
            "PostgreSQL URL (default: reads POSTGRES_URL env var, "
            "fallback: postgresql://brain:brain@localhost:5433/brain). "
            "Note: +asyncpg suffix is stripped automatically."
        ),
    )
    parser.add_argument(
        "--service-url",
        default=None,
        help=(
            "GPU embedding service URL (default: reads EMBEDDING_SERVICE_URL env var, "
            "fallback: http://localhost:8003)."
        ),
    )
    return parser.parse_args()


async def main() -> int:
    """Main async entry point. Returns exit code (0=success, 1=errors)."""
    args = parse_args()

    # Resolve entity types
    if args.entity_types:
        entity_types = [e.strip() for e in args.entity_types.split(",") if e.strip()]
        invalid = [e for e in entity_types if e not in ENTITY_TYPES]
        if invalid:
            print(f"ERROR: Unknown entity types: {invalid}")
            print(f"Valid types: {ENTITY_TYPES}")
            return 1
    else:
        entity_types = list(ENTITY_TYPES)

    # Parse --since into a date (asyncpg requires a real date/datetime).
    since_date: dt.date | None = None
    if args.since is not None:
        try:
            since_date = dt.date.fromisoformat(args.since)
        except ValueError as exc:
            print(f"ERROR: invalid --since value {args.since!r}: {exc}")
            return 1

    # Resolve URLs
    postgres_url = args.postgres_url or os.environ.get(
        "POSTGRES_URL", "postgresql://brain:brain@localhost:5433/brain"
    )
    postgres_url = clean_postgres_url(postgres_url)

    service_url = args.service_url or os.environ.get(
        "EMBEDDING_SERVICE_URL", "http://localhost:8003"
    )

    # Print configuration
    print("=" * 60)
    print("brain_v42 — Embedding Regeneration")
    print("=" * 60)
    print(f"  PostgreSQL:  {postgres_url}")
    print(f"  GPU Service: {service_url}")
    print(f"  Entities:    {', '.join(entity_types)}")
    print(f"  Batch size:  {args.batch_size}")
    print(f"  Dry run:     {args.dry_run}")
    print(f"  Only missing:{args.only_missing}")
    print(f"  Project key: {args.project_key or '(all)'}")
    print(f"  Since:       {args.since or '(all)'}")
    print("=" * 60)

    # Connect to PostgreSQL
    try:
        pool = await asyncpg.create_pool(postgres_url, min_size=1, max_size=5)
    except Exception as exc:
        print(f"ERROR: Failed to connect to PostgreSQL: {exc}")
        return 1

    start_time = time.perf_counter()

    try:
        # Process each entity type
        summaries: dict[str, dict[str, int]] = {}

        async with httpx.AsyncClient() as client:
            # Quick health check on GPU service (skip in dry-run)
            if not args.dry_run:
                try:
                    resp = await client.get(f"{service_url}/healthz", timeout=5.0)
                    resp.raise_for_status()
                    print("GPU service health: OK")
                except Exception as exc:
                    print(f"WARNING: GPU service health check failed: {exc}")
                    print("Continuing anyway — batches will fail individually if service is down.")

            for entity_type in entity_types:
                summary = await process_entity_type(
                    pool=pool,
                    client=client,
                    service_url=service_url,
                    entity_type=entity_type,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    only_missing=args.only_missing,
                    project_key=args.project_key,
                    since=since_date,
                )
                summaries[entity_type] = summary

        elapsed = time.perf_counter() - start_time

        # Print summary table
        print()
        print("-" * 60)
        header = f"{'Entity':<15} {'Total':<10} {'Success':<10} {'Errors':<10} {'Skipped':<10}"
        print(header)
        print("-" * 60)

        total_all = 0
        success_all = 0
        errors_all = 0
        skipped_all = 0

        for entity_type in entity_types:
            s = summaries[entity_type]
            print(
                f"{entity_type:<15} {s['total']:<10} {s['success']:<10} "
                f"{s['errors']:<10} {s['skipped']:<10}"
            )
            total_all += s["total"]
            success_all += s["success"]
            errors_all += s["errors"]
            skipped_all += s["skipped"]

        print("-" * 60)
        print(f"{'TOTAL':<15} {total_all:<10} {success_all:<10} {errors_all:<10} {skipped_all:<10}")
        print(f"\nCompleted in {elapsed:.1f}s")

        if errors_all > 0:
            print(f"\nWARNING: {errors_all} errors occurred.")
            return 1

        return 0

    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
