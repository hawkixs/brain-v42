#!/usr/bin/env python3
"""Prove the configured embedding model is the one that wrote the corpus.

Nothing in the schema records which model produced a vector. Qodo-Embed and
codestral-embed both emit 1536 dimensions, so repointing
``BRAIN_EMBEDDING_MODEL`` without a full reindex raises no error anywhere:
pgvector keeps answering and every cosine distance silently becomes noise.

This script measures instead of asking. It samples already-embedded rows,
rebuilds each one's text through the SAME canonical composer the write path
uses (``embedding_text_from_row`` -- shared by learning_service,
decision_service and the backfill), re-embeds it through the SAME factory
every runtime builds its client from, and compares the fresh vector with the
stored one. A declared fingerprint would only prove what a config file claims;
this proves what the corpus contains.

Exit codes:
    0  match         -- the configured model reproduces the stored vectors
    1  drift         -- the corpus was written by a different model; reindex
    2  unmeasurable  -- no sample, provider down, or database unreachable

Run it before arming a provider switch, after the reindex that follows one,
and from the switch runbook on both sides of the cutover.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys

import asyncpg
import structlog

from brain_v42.services.embedding_drift import (
    DEFAULT_THRESHOLD,
    DriftReport,
    SampleComparison,
    classify_drift,
    final_exit_code,
)
from brain_v42.services.embedding_factory import (
    build_embedding_service,
    settings_for_standalone_script,
)
from brain_v42.services.embedding_text import (
    GITLAB_EVENT_TITLE_MAX_CHARS,
    ReproducibleEntityType,
    reproducible_embedding_text,
)
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable, GPUEmbeddingService

#: Every vector table, and the columns its text is recomposed from.
#:
#: It used to be six. The other three -- `indexed_plans`, `indexed_plan_chunks`
#: and `gitlab_events` -- held 2239 of 8811 embedded rows and no composer could
#: reach them, so a provider switch could leave a quarter of the corpus on the
#: old model while this check reported MATCH. `indexed_plan_chunks` alone is
#: 1792 rows and is served by `brain_search`.
#:
#: They are covered now because the write paths and this checker call the SAME
#: composers, in `brain_v42.services.embedding_text`. Composing the text here a
#: second way would report drift on columns nobody had touched.
SAMPLED_TABLES: dict[ReproducibleEntityType, tuple[str, tuple[str, ...]]] = {
    "decision": ("decisions", ("title", "description", "reasoning")),
    "learning": ("learnings", ("topic", "insight")),
    "snippet": ("snippets", ("intention",)),
    "runbook": ("runbooks", ("title", "description", "trigger")),
    "adr": ("adrs", ("title", "context", "decision")),
    "feature": ("features", ("description",)),
    "plan": ("indexed_plans", ("title", "summary", "content")),
    "plan_chunk": ("indexed_plan_chunks", ("content",)),
    "gitlab_event": ("gitlab_events", ("title",)),
}

#: Rows whose own columns cannot reproduce what was embedded from them, counted
#: over the WHOLE table rather than the sample. `gitlab_events` embeds
#: `text[:2000]` and stores `text[:500]`: measured 2026-09-22, 127 of its 239
#: rows sit at the storage ceiling, so more than half the table is unverifiable
#: by construction and no amount of sampling changes that. Closing it needs a
#: wider column, not a better checker -- and the table is dead by decision
#: `218028c7`, so the honest move is to name the hole, not to hide it.
UNVERIFIABLE_ROWS: dict[str, tuple[str, str]] = {
    "gitlab_events": (
        "SELECT count(*) FROM gitlab_events "
        f"WHERE embedding IS NOT NULL AND length(title) >= {GITLAB_EVENT_TITLE_MAX_CHARS}",
        "the stored title is a shorter truncation than the embedded text",
    ),
}


async def count_unverifiable(conn: asyncpg.Connection | None) -> list[dict[str, object]]:
    """Rows the checker refuses to judge, with the reason it refuses."""
    entries: list[dict[str, object]] = []
    for table, (query, reason) in UNVERIFIABLE_ROWS.items():
        rows: int | None = None
        if conn is not None:
            rows = await conn.fetchval(query)
        entries.append({"table": table, "rows": rows, "reason": reason})
    return entries


def parse_pgvector(raw: str) -> list[float]:
    """Parse pgvector's text form ``[0.1,0.2,…]`` into floats.

    asyncpg has no built-in codec for the extension type, so the query casts
    the column to text and the parsing happens here rather than through a
    registered codec -- one script should not have to teach the driver a type.
    """
    return [float(part) for part in raw.strip().strip("[]").split(",") if part]


def cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, normalising both sides.

    The shim L2-normalises server-side and a hosted provider may or may not, so
    neither vector is assumed to be unit length: an un-normalised pair would
    otherwise score above 1.0 and be refused by `classify_drift` as a parsing
    bug.
    """
    if len(left) != len(right):
        raise ValueError(f"dimension mismatch: stored {len(left)} vs fresh {len(right)}")
    left_norm = math.sqrt(sum(v * v for v in left))
    right_norm = math.sqrt(sum(v * v for v in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (left_norm * right_norm)


async def fetch_sample(
    conn: asyncpg.Connection,
    entity_type: ReproducibleEntityType,
    per_type: int,
) -> list[dict[str, object]]:
    """Random already-embedded rows of one type, with their stored vector."""
    table, columns = SAMPLED_TABLES[entity_type]
    selected = ", ".join(f'"{column}"' for column in columns)
    rows = await conn.fetch(
        f"SELECT id::text AS id, {selected}, embedding::text AS stored "  # noqa: S608
        f"FROM {table} WHERE embedding IS NOT NULL ORDER BY random() LIMIT $1",
        per_type,
    )
    return [dict(row) for row in rows]


async def compare(
    conn: asyncpg.Connection,
    embedding_svc: GPUEmbeddingService,
    per_type: int,
) -> tuple[list[SampleComparison], list[str]]:
    """Re-embed each sampled row and score it against its stored vector."""
    comparisons: list[SampleComparison] = []
    problems: list[str] = []

    for entity_type in SAMPLED_TABLES:
        rows = await fetch_sample(conn, entity_type, per_type)
        if not rows:
            continue

        texts: list[str] = []
        kept: list[dict[str, object]] = []
        refused = 0
        for row in rows:
            text = reproducible_embedding_text(entity_type, row)
            if text is None:
                # The row does not contain what was embedded from it. Never a
                # match, never drift: a guess here would accuse a vector nobody
                # touched.
                refused += 1
                continue
            if not text.strip():
                continue
            kept.append(row)
            texts.append(text)
        if refused:
            problems.append(
                f"{entity_type}: {refused} of {len(rows)} sampled rows cannot reproduce "
                f"their own embedding input"
            )
        if not kept:
            continue

        try:
            fresh = await embedding_svc.embed_texts(texts)
        except EmbeddingUnavailable as exc:
            problems.append(f"{entity_type}: embedding endpoint unavailable ({exc.kind})")
            continue

        for row, vector in zip(kept, fresh, strict=True):
            try:
                similarity = cosine(parse_pgvector(str(row["stored"])), vector)
            except ValueError as exc:
                problems.append(f"{entity_type} {row['id']}: {exc}")
                continue
            comparisons.append(
                SampleComparison(
                    entity_type=entity_type,
                    entity_id=str(row["id"]),
                    similarity=similarity,
                )
            )

    return comparisons, problems


def render(
    report: DriftReport,
    problems: list[str],
    backend: str,
    model: str,
    unverifiable: list[dict[str, object]],
) -> str:
    lines = [
        "brain_v42 — embedding model drift check",
        f"  configured backend : {backend}",
        f"  configured model   : {model}",
        f"  sampled rows       : {report.sampled}",
        f"  threshold          : {report.threshold}",
    ]
    if report.median is not None:
        lines += [
            f"  median similarity  : {report.median:.4f}",
            f"  min / max          : {report.minimum:.4f} / {report.maximum:.4f}",
            f"  rows below thresh. : {report.outliers}",
        ]
    for breakdown in report.by_type:
        flag = (
            "  <-- below threshold"
            if (breakdown.median is not None and breakdown.median < report.threshold)
            else ""
        )
        median = "n/a" if breakdown.median is None else f"{breakdown.median:.4f}"
        lines.append(
            f"    {breakdown.entity_type:<9} n={breakdown.sampled:<3} median={median}"
            f"  below={breakdown.outliers}{flag}"
        )
    lines.append(f"  VERDICT            : {report.verdict.value.upper()}")
    if report.verdict.value == "drift":
        lines.append(
            "  → the stored vectors were not produced by this model. "
            "Searching now returns noise; reindex before serving."
        )
    if report.verdict.value == "unmeasurable":
        lines.append("  → nothing was measured; this is not a pass.")
    lines += [f"  ! {problem}" for problem in problems]
    total = sum(int(entry["rows"] or 0) for entry in unverifiable if entry["rows"])
    if total:
        lines.append(f"  UNVERIFIABLE       : {total} embedded rows")
        for entry in unverifiable:
            if entry["rows"]:
                lines.append(f"    {entry['table']}: {entry['rows']} — {entry['reason']}")
        lines.append("                       (a wider column would close this, not a better check)")
    else:
        lines.append("  UNVERIFIABLE       : none — every embedded row can reproduce its input")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    # structlog's default logger prints to stdout, and `build_embedding_service`
    # logs one line on every call. Left alone it interleaves with the `--json`
    # document and every consumer's parse fails -- the same stdout-pollution
    # that silently empties an MCP stdio server's tool list. Diagnostics belong
    # on stderr whatever the mode, so the redirect is unconditional.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

    settings = settings_for_standalone_script(args.postgres_url)
    embedding_svc = build_embedding_service(settings)
    dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    comparisons: list[SampleComparison] = []
    problems: list[str] = []
    unverifiable = await count_unverifiable(None)

    # An unreachable database is reported THROUGH the report, not instead of it:
    # a runbook branching on `--json` must still receive a document saying
    # `unmeasurable`, rather than an empty stdout and a trace on stderr.
    try:
        conn = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        problems.append(f"cannot reach PostgreSQL: {exc}")
    else:
        try:
            comparisons, problems = await compare(conn, embedding_svc, args.per_type)
            unverifiable = await count_unverifiable(conn)
        finally:
            await conn.close()

    report = classify_drift(comparisons, threshold=args.threshold)

    if args.json:
        print(
            json.dumps(
                {
                    "backend": settings.embedding_backend,
                    "model": settings.embedding_model,
                    "verdict": report.verdict.value,
                    "threshold": report.threshold,
                    "sampled": report.sampled,
                    "outliers": report.outliers,
                    "median": report.median,
                    "minimum": report.minimum,
                    "maximum": report.maximum,
                    "by_type": [
                        {
                            "entity_type": b.entity_type,
                            "sampled": b.sampled,
                            "median": b.median,
                            "outliers": b.outliers,
                        }
                        for b in report.by_type
                    ],
                    "problems": problems,
                    "unverifiable": unverifiable,
                },
                indent=2,
            )
        )
    else:
        print(
            render(
                report,
                problems,
                settings.embedding_backend,
                settings.embedding_model,
                unverifiable,
            )
        )

    return final_exit_code(report, problems)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-url",
        default="postgresql://brain@localhost:5433/brain",
        help="Fallback DSN when POSTGRES_URL is absent from the environment.",
    )
    parser.add_argument(
        "--per-type",
        type=int,
        default=8,
        help="Rows sampled per knowledge type (default 8, so 40 rows over five types).",
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
