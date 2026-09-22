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

No mode deletes a plan, chunk or feature. `--apply` rewrites ownership/path
or archives a plan, preserving its stored text and vectors. The explicit
`--detach-cross-project-links` option can remove foreign feature-artifact
links for touched plans after saving those complete links in recovery.

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
import os
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

_PLAN_SUFFIXES = ("-design.md", "-plan.md")

logger = structlog.get_logger(__name__)


class InventoryApplyError(RuntimeError):
    """Fail-closed operational error whose text contains no connection data."""


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


def walk_disk(roots: dict[str, list[Path]], issues: list[str] | None = None) -> list[DiskPlanFile]:
    """Every real plan file under a canonical scan root, with its content hash."""
    found: dict[str, DiskPlanFile] = {}
    for project_key, project_roots in roots.items():
        for root in project_roots:

            def onerror(error: OSError, scan_root: Path = root) -> None:
                path = str(error.filename or scan_root)
                if issues is None:
                    raise InventoryApplyError("scan_walk_error")
                issues.append(path)

            for directory, _dirs, filenames in os.walk(
                root, topdown=True, onerror=onerror, followlinks=False
            ):
                for filename in filenames:
                    if not filename.endswith(_PLAN_SUFFIXES):
                        continue
                    candidate = Path(directory, filename)
                    # A runtime scan never follows a symlinked plan file. Its
                    # target can be outside the configured root, and accepting
                    # it would turn an ownership inventory into a path escape.
                    if candidate.is_symlink():
                        continue
                    try:
                        canonical = candidate.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    try:
                        canonical.relative_to(root)
                    except ValueError:
                        continue
                    if not canonical.is_file():
                        continue
                    previous = found.get(str(canonical))
                    if previous is not None:
                        if previous.project_key != project_key:
                            raise InventoryApplyError("cross_project_path_claim")
                        continue
                    digest = _hash_file(canonical)
                    if digest is None:
                        if issues is not None:
                            issues.append(str(canonical))
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
        "freshness_status, freshness_source, indexed_at, updated_at FROM indexed_plans ORDER BY id"
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
            freshness_source=record["freshness_source"],
            updated_at=record["updated_at"].isoformat(),
        )
        for record in records
    ]


async def read_scan_paths(conn: asyncpg.Connection) -> dict[str, list[str]]:
    records = await conn.fetch(
        "SELECT project_key, plan_scan_paths FROM project_contexts "
        "WHERE plan_scan_paths IS NOT NULL AND plan_scan_paths <> '{}'"
    )
    return {record["project_key"]: list(record["plan_scan_paths"]) for record in records}


async def _write_mutations(conn: asyncpg.Connection, mutations: tuple) -> int:
    """Write every mutation in ONE transaction, or none of them.

    A partial repair is worse than no repair: half the rows rewritten and half
    still wrong is a state nobody measured and no report describes.
    """
    for mutation in mutations:
        if mutation.kind == "rewrite":
            await conn.execute(
                "UPDATE indexed_plans SET file_path = $2, project_key = $3, "
                "updated_at = NOW() WHERE id = $1::uuid",
                mutation.row_id,
                mutation.after["file_path"],
                mutation.after["project_key"],
            )
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


async def apply_mutations(conn: asyncpg.Connection, mutations: tuple) -> int:
    """Compatibility wrapper for direct callers that only need atomic writes."""
    async with conn.transaction():
        return await _write_mutations(conn, mutations)


def _locked_row_matches(row: dict[str, object], mutation: object) -> bool:
    before = mutation.before
    return all(key in row and _serial_value(row[key]) == value for key, value in before.items())


def _serial_value(value: object) -> object:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


def _record_dict(record: object) -> dict[str, object]:
    return {key: _serial_value(value) for key, value in dict(record).items()}


async def apply_with_recovery(
    conn: asyncpg.Connection,
    mutations: tuple,
    recovery_path: Path,
    *,
    expected_dependents: dict[str, list[dict[str, object]]],
    detach_cross_project_links: bool,
) -> tuple[int, str]:
    """Lock, revalidate, snapshot and mutate exactly one approved plan set."""
    plan_ids = [mutation.row_id for mutation in mutations]
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        # Row locks do not prevent a concurrent feature linker from inserting a
        # new edge after its predicate scan. These bounded relation locks cover
        # the touched dependent tables until this transaction commits; writers
        # still have to be quiescent for the linker's later transaction.
        await conn.execute(
            "LOCK TABLE indexed_plan_chunks, feature_artifacts IN SHARE ROW EXCLUSIVE MODE"
        )
        plans = [
            _record_dict(row)
            for row in await conn.fetch(
                "SELECT id::text AS id, file_path, project_key, content_hash, "
                "freshness_status, freshness_source, indexed_at, updated_at FROM indexed_plans "
                "WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
                plan_ids,
            )
        ]
        by_id = {row["id"]: row for row in plans}
        if len(by_id) != len(mutations) or any(
            not _locked_row_matches(by_id.get(mutation.row_id, {}), mutation)
            for mutation in mutations
        ):
            raise InventoryApplyError("plan_state_changed")
        chunks = [
            _record_dict(row)
            for row in await conn.fetch(
                "SELECT id::text AS id, plan_id::text AS plan_id, project_key "
                "FROM indexed_plan_chunks WHERE plan_id = ANY($1::uuid[]) "
                "ORDER BY plan_id, id FOR UPDATE",
                plan_ids,
            )
        ]
        edges = [
            _record_dict(row)
            for row in await conn.fetch(
                "SELECT fa.feature_id::text AS feature_id, fa.artifact_type, "
                "fa.artifact_id::text AS artifact_id, fa.similarity_score, "
                "fa.created_at, f.project_key AS feature_project_key "
                "FROM feature_artifacts AS fa JOIN features AS f ON f.id = fa.feature_id "
                "WHERE fa.artifact_type = 'plan' AND fa.artifact_id = ANY($1::uuid[]) "
                "ORDER BY fa.artifact_id, fa.feature_id FOR UPDATE OF fa, f",
                plan_ids,
            )
        ]
        if {"chunks": chunks, "feature_edges": edges} != expected_dependents:
            raise InventoryApplyError("dependent_state_changed")
        final_projects = {mutation.row_id: mutation.after["project_key"] for mutation in mutations}
        foreign_edges = [
            edge
            for edge in edges
            if edge["feature_project_key"] != final_projects[edge["artifact_id"]]
        ]
        if foreign_edges and not detach_cross_project_links:
            raise InventoryApplyError("cross_project_feature_link")
        digest = write_private_json(
            recovery_path,
            {
                "version": 2,
                "mutations": [
                    {"row_id": m.row_id, "kind": m.kind, "before": m.before, "after": m.after}
                    for m in mutations
                ],
                "plans": plans,
                "chunks": chunks,
                "feature_edges": edges,
            },
        )
        for edge in foreign_edges:
            await conn.execute(
                "DELETE FROM feature_artifacts WHERE feature_id = $1::uuid "
                "AND artifact_type = 'plan' AND artifact_id = $2::uuid",
                edge["feature_id"],
                edge["artifact_id"],
            )
        return await _write_mutations(conn, mutations), digest


async def read_dependent_state(
    conn: asyncpg.Connection, plan_ids: list[str]
) -> dict[str, list[dict[str, object]]]:
    """Approved dependent baseline; never refreshed after the dry inventory."""
    chunks = [
        _record_dict(row)
        for row in await conn.fetch(
            "SELECT id::text AS id, plan_id::text AS plan_id, project_key "
            "FROM indexed_plan_chunks WHERE plan_id = ANY($1::uuid[]) ORDER BY plan_id, id",
            plan_ids,
        )
    ]
    edges = [
        _record_dict(row)
        for row in await conn.fetch(
            "SELECT fa.feature_id::text AS feature_id, fa.artifact_type, "
            "fa.artifact_id::text AS artifact_id, fa.similarity_score, fa.created_at, "
            "f.project_key AS feature_project_key FROM feature_artifacts AS fa "
            "JOIN features AS f ON f.id = fa.feature_id WHERE fa.artifact_type = 'plan' "
            "AND fa.artifact_id = ANY($1::uuid[]) ORDER BY fa.artifact_id, fa.feature_id",
            plan_ids,
        )
    ]
    return {"chunks": chunks, "feature_edges": edges}


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
    except (OSError, asyncpg.PostgresError):
        print("cannot reach PostgreSQL", file=sys.stderr)
        return 2

    try:
        configured = await read_scan_paths(conn)
        roots, duplicates, unresolvable = collapse_scan_roots(configured)
        if not any(roots.values()):
            print("no scan path resolves to a directory; nothing measurable", file=sys.stderr)
            return 2
        scan_issues: list[str] = []
        try:
            disk_files = walk_disk(roots, scan_issues)
            rows = await read_rows(conn)
            decisions = classify(rows, disk_files)
        except (InventoryApplyError, ValueError):
            print("cannot build a consistent plan inventory", file=sys.stderr)
            return 2
        mutations = plan_mutations(decisions)

        if args.verify:
            if unresolvable or scan_issues:
                print("refusing: scan inventory is incomplete", file=sys.stderr)
                return 2
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

        if unresolvable or scan_issues:
            print(
                "refusing: scan inventory is incomplete",
                file=sys.stderr,
            )
            return 2

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
        try:
            expected_dependents = await read_dependent_state(
                conn, [mutation.row_id for mutation in mutations]
            )
            written, digest = await apply_with_recovery(
                conn,
                mutations,
                recovery_path,
                expected_dependents=expected_dependents,
                detach_cross_project_links=args.detach_cross_project_links,
            )
        except (InventoryApplyError, asyncpg.PostgresError):
            print("refusing: locked state changed or could not be locked", file=sys.stderr)
            return 2
        print(f"recovery written to {recovery_path} (sha256 {digest})", file=sys.stderr)
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
    parser.add_argument(
        "--detach-cross-project-links",
        action="store_true",
        help="Detach only foreign feature links for the selected plan mutations.",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
