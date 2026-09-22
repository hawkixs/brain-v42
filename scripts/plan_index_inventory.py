#!/usr/bin/env python3
"""Say what every indexed plan row IS, before anything is allowed to touch it.

208 rows, measured 2026-09-22: 70 point at a real file under their own
project, 70 point at a name the scanner no longer produces while their content
sits on disk, and 68 point at nothing at all. Two defects put them there --
a relative scan path resolved against the service's working directory, and a
scan root reached through a symlink that the traversal canonicalised -- and
the rows they left look alike from the database alone.

They are not alike. A row whose content is still on disk is MIS-FILED and gets
rewritten. A row with nothing to point at is the last remaining copy of that
plan. The ticket called both "orphelines", which is exactly what made the
repair sound like a delete.

NOTHING HERE DELETES, in any mode. `--apply` issues two kinds of statement,
`rewrite` and `archive`. Archiving sets `freshness_status='archived'`, which
`indexed_plan_search_service` already filters out of every default view, and
leaves every byte where it is.

Read-only unless `--apply` is passed, and `--apply` refuses to start without a
recovery file it wrote itself.

Exit codes:
    0  nothing to do (or, with --verify, every project balances)
    1  repairs are pending (or a project does not balance)
    2  could not measure -- database unreachable, no scan path resolvable
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg
import structlog

from brain_v42.maintenance.plan_index_inventory import (
    DiskPlanFile,
    PlanRow,
    PlanRowVerdict,
    RowDecision,
    classify,
    plan_mutations,
    verify_projects,
)
from brain_v42.maintenance.plan_index_repair import write_private_json

#: Same ceiling the repair boundary uses. A plan is a markdown document; a file
#: past this is not one, and hashing it would be the slow way to find out.
MAX_PLAN_FILE_BYTES = 8 * 1024 * 1024

#: An apply beyond this many rows is a different operation than the one this
#: script was reviewed for. Fail closed and make the operator say so.
DEFAULT_MAX_MUTATIONS = 500

_PLAN_GLOBS = ("**/*-design.md", "**/*-plan.md")

logger = structlog.get_logger(__name__)


def _hash_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_PLAN_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def collapse_scan_roots(
    configured: dict[str, list[str]],
) -> tuple[dict[str, list[Path]], list[tuple[str, str, list[str]]], list[tuple[str, str]]]:
    """Canonicalise each project's scan paths and report the duplicates.

    Acceptance criterion 3, decided here: the traversal keeps canonicalising,
    and a configuration that declares two names for one directory is collapsed
    and reported. The alternative -- storing the configured name -- cannot
    work: `indexed_plans.file_path` is UNIQUE table-wide, so two names for one
    file means two rows for one plan, which is the duplication being repaired.

    Returns (roots per project, duplicate declarations, unresolvable paths).
    """
    roots: dict[str, list[Path]] = {}
    duplicates: list[tuple[str, str, list[str]]] = []
    unresolvable: list[tuple[str, str]] = []

    for project_key, paths in sorted(configured.items()):
        seen: dict[Path, list[str]] = defaultdict(list)
        for raw in paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                unresolvable.append((project_key, raw))
                continue
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                unresolvable.append((project_key, raw))
                continue
            if not canonical.is_dir():
                unresolvable.append((project_key, raw))
                continue
            seen[canonical].append(raw)
        for canonical, declared in seen.items():
            if len(declared) > 1:
                duplicates.append((project_key, str(canonical), sorted(declared)))
        roots[project_key] = sorted(seen)
    return roots, duplicates, unresolvable


def walk_disk(roots: dict[str, list[Path]]) -> list[DiskPlanFile]:
    """Every real plan file under a canonical scan root, with its content hash."""
    found: dict[str, DiskPlanFile] = {}
    for project_key, project_roots in roots.items():
        for root in project_roots:
            for pattern in _PLAN_GLOBS:
                for candidate in root.glob(pattern):
                    try:
                        canonical = candidate.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if not canonical.is_file() or str(canonical) in found:
                        continue
                    digest = _hash_file(canonical)
                    if digest is None:
                        continue
                    found[str(canonical)] = DiskPlanFile(
                        path=str(canonical),
                        project_key=project_key,
                        content_hash=digest,
                    )
    return sorted(found.values(), key=lambda f: f.path)


def _resolved(file_path: str) -> str | None:
    candidate = Path(file_path)
    if not candidate.is_absolute():
        return None
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return str(canonical) if canonical.is_file() else None


async def read_rows(conn: asyncpg.Connection) -> list[PlanRow]:
    records = await conn.fetch(
        "SELECT id::text AS id, project_key, file_path, content_hash, "
        "freshness_status, indexed_at FROM indexed_plans ORDER BY id"
    )
    return [
        PlanRow(
            row_id=record["id"],
            project_key=record["project_key"],
            file_path=record["file_path"],
            content_hash=record["content_hash"],
            freshness_status=record["freshness_status"],
            indexed_at=record["indexed_at"].isoformat(),
            resolved_path=_resolved(record["file_path"]),
        )
        for record in records
    ]


async def read_scan_paths(conn: asyncpg.Connection) -> dict[str, list[str]]:
    records = await conn.fetch(
        "SELECT project_key, plan_scan_paths FROM project_contexts "
        "WHERE plan_scan_paths IS NOT NULL AND plan_scan_paths <> '{}'"
    )
    return {record["project_key"]: list(record["plan_scan_paths"]) for record in records}


async def apply_mutations(conn: asyncpg.Connection, mutations: tuple) -> int:
    """Write every mutation in ONE transaction, or none of them.

    A partial repair is worse than no repair: half the rows rewritten and half
    still wrong is a state nobody measured and no report describes.
    """
    async with conn.transaction():
        for mutation in mutations:
            if mutation.kind == "rewrite":
                await conn.execute(
                    "UPDATE indexed_plans SET file_path = $2, project_key = $3, "
                    "updated_at = NOW() WHERE id = $1::uuid",
                    mutation.row_id,
                    mutation.after["file_path"],
                    mutation.after["project_key"],
                )
                # Chunks carry their own project_key and are searched on it.
                # Leaving them behind would file a plan under one project and
                # its sections under another.
                await conn.execute(
                    "UPDATE indexed_plan_chunks SET project_key = $2 WHERE plan_id = $1::uuid",
                    mutation.row_id,
                    mutation.after["project_key"],
                )
            else:
                await conn.execute(
                    "UPDATE indexed_plans SET freshness_status = 'archived', "
                    "freshness_source = 'manual_update', updated_at = NOW() "
                    "WHERE id = $1::uuid",
                    mutation.row_id,
                )
    return len(mutations)


def render(
    decisions: tuple[RowDecision, ...],
    duplicates: list[tuple[str, str, list[str]]],
    unresolvable: list[tuple[str, str]],
) -> str:
    counts: dict[str, int] = defaultdict(int)
    for decision in decisions:
        counts[decision.verdict.value] += 1

    lines = ["Plan index inventory", "=" * 60, ""]
    for verdict in PlanRowVerdict:
        lines.append(f"  {counts.get(verdict.value, 0):>5}  {verdict.value}")
    lines.append(f"  {len(decisions):>5}  rows total")
    lines.append("")

    per_reason: dict[str, int] = defaultdict(int)
    for decision in decisions:
        if decision.verdict is not PlanRowVerdict.CANONICAL:
            per_reason[f"{decision.verdict.value}: {decision.reason}"] += 1
    if per_reason:
        lines.append("Why:")
        for reason, count in sorted(per_reason.items()):
            lines.append(f"  {count:>5}  {reason}")
        lines.append("")

    if duplicates:
        lines.append("Scan paths declaring two names for one directory:")
        for project_key, canonical, declared in duplicates:
            lines.append(f"  [{project_key}] {canonical}")
            for raw in declared:
                lines.append(f"      declared as {raw}")
        lines.append("")
    if unresolvable:
        lines.append("Scan paths that resolve to nothing (never scanned):")
        for project_key, raw in unresolvable:
            lines.append(f"  [{project_key}] {raw}")
        lines.append("")
    return "\n".join(lines)


def render_verification(verifications: tuple) -> str:
    lines = ["Rows against real files, per project", "=" * 60, ""]
    lines.append(f"  {'project':<24}{'rows':>7}{'files':>7}   verdict")
    for verification in verifications:
        mark = "ok" if verification.matches else "MISMATCH"
        lines.append(
            f"  {verification.project_key:<24}{verification.indexed_rows:>7}"
            f"{verification.disk_files:>7}   {mark}"
        )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    # Diagnostics on stderr whatever the mode, so `--json` stays parseable.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

    dsn = args.postgres_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        conn = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"cannot reach PostgreSQL: {exc}", file=sys.stderr)
        return 2

    try:
        configured = await read_scan_paths(conn)
        roots, duplicates, unresolvable = collapse_scan_roots(configured)
        if not any(roots.values()):
            print("no scan path resolves to a directory; nothing measurable", file=sys.stderr)
            return 2
        disk_files = walk_disk(roots)
        rows = await read_rows(conn)
        decisions = classify(rows, disk_files)
        mutations = plan_mutations(decisions)

        if args.verify:
            verifications = verify_projects(rows, disk_files)
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "project_key": v.project_key,
                                "indexed_rows": v.indexed_rows,
                                "disk_files": v.disk_files,
                                "matches": v.matches,
                            }
                            for v in verifications
                        ],
                        indent=2,
                    )
                )
            else:
                print(render_verification(verifications))
            return 0 if all(v.matches for v in verifications) else 1

        if args.json:
            print(
                json.dumps(
                    {
                        "rows": len(rows),
                        "disk_files": len(disk_files),
                        "duplicate_scan_declarations": [
                            {"project_key": p, "canonical": c, "declared": d}
                            for p, c, d in duplicates
                        ],
                        "unresolvable_scan_paths": [
                            {"project_key": p, "path": raw} for p, raw in unresolvable
                        ],
                        "decisions": [
                            {
                                "row_id": d.row.row_id,
                                "project_key": d.row.project_key,
                                "file_path": d.row.file_path,
                                "verdict": d.verdict.value,
                                "reason": d.reason,
                                "target_path": d.target_path,
                                "target_project": d.target_project,
                            }
                            for d in decisions
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(render(decisions, duplicates, unresolvable))
            print(f"{len(mutations)} mutation(s) pending. Dry run: nothing was written.")

        if not args.apply:
            return 0 if not mutations else 1

        if len(mutations) > args.max_mutations:
            print(
                f"refusing: {len(mutations)} mutations exceeds --max-mutations "
                f"{args.max_mutations}",
                file=sys.stderr,
            )
            return 2

        recovery_path = Path(args.recovery_file)
        if recovery_path.exists():
            print(f"refusing: {recovery_path} already exists", file=sys.stderr)
            return 2
        digest = write_private_json(
            recovery_path,
            {
                "version": 1,
                "mutations": [
                    {
                        "row_id": m.row_id,
                        "kind": m.kind,
                        "before": m.before,
                        "after": m.after,
                    }
                    for m in mutations
                ],
            },
        )
        print(f"recovery written to {recovery_path} (sha256 {digest})", file=sys.stderr)

        written = await apply_mutations(conn, mutations)
        print(f"applied {written} mutation(s) in one transaction", file=sys.stderr)
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-url",
        default="postgresql://brain@localhost:5433/brain",
        help="Fallback DSN when POSTGRES_URL is absent from the environment.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Acceptance criterion 4: live rows against real files, per project.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the mutations. Refuses without a fresh recovery file.",
    )
    parser.add_argument(
        "--recovery-file",
        default="plan-index-recovery.json",
        help="Where the pre-mutation state is written before --apply.",
    )
    parser.add_argument("--max-mutations", type=int, default=DEFAULT_MAX_MUTATIONS)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
