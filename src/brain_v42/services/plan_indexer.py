"""PlanIndexer — scans spec/plan files, indexes them, links to features.

Scans directories for files matching *-design.md and *-plan.md patterns.
For each file:
  1. Parse title (frontmatter > H1 heading > filename)
  2. Compute SHA256 content hash
  3. Skip if hash matches DB (unchanged)
  4. Chunk markdown via chunk_markdown() -> (parent, chunks)
  5. Embed parent + each chunk
  6. Upsert into indexed_plans + indexed_plan_chunks tables
  7. Call ClusterGuard.resolve() to find/create feature (on parent)
  8. Link plan to feature via feature_artifacts
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import feature_artifacts, indexed_plans, project_contexts
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.embedding_text import (
    indexed_plan_chunk_embedding_text,
    indexed_plan_embedding_text,
    truncate_plan_embed_input,
)
from brain_v42.services.plan_chunker import chunk_markdown

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from brain_v42.services.cluster_guard import ClusterGuard
    from brain_v42.services.gpu_embedding_service import GPUEmbeddingService

logger = structlog.get_logger(__name__)


class ReindexVerdict(NamedTuple):
    """Why a plan file is or is not reprocessed.

    ``content_identical`` exists so the caller can separate a vector refresh
    from a new signal. Without it the two are indistinguishable at the call
    site, and a provider switch -- which marks every plan stale precisely to
    force a re-embed -- looks exactly like 208 edited files.
    """

    skip: bool
    content_identical: bool


# Glob patterns to match plan/spec files
_DESIGN_SUFFIX = "-design.md"
_PLAN_SUFFIX = "-plan.md"

# Regex for YAML frontmatter
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FM_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_FM_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)

# Regex for first H1 heading
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Regex for date prefix in filenames (e.g., 2026-03-14-)
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

# Max batch size per embed_texts call. The Qodo-Embed-1-1.5B model on a
# 6 GiB GPU runs out of memory on large batches when inputs are large.
# A batch size of 2 keeps peak memory bounded while still benefiting from
# batching on small inputs.
_EMBED_BATCH_SIZE = 2
_SCAN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SCAN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class PlanScanPathError(ValueError):
    """Closed failure raised before an unsafe plan scan can start."""

    def __init__(self, path: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.path = path
        self.reason_code = reason_code


async def _embed_in_batches(embedding_svc: Any, inputs: list[str]) -> list[list[float]]:
    """Embed inputs in sub-batches to avoid GPU OOM on large plans."""
    out: list[list[float]] = []
    if hasattr(embedding_svc, "embed_texts"):
        for i in range(0, len(inputs), _EMBED_BATCH_SIZE):
            sub = inputs[i : i + _EMBED_BATCH_SIZE]
            out.extend(await embedding_svc.embed_texts(sub))
    else:
        for inp in inputs:
            out.append(await embedding_svc.embed(inp))
    return out


def _read_scan_file(scan_root: str, file_path: Path) -> str:
    """Read one plan through no-follow directory descriptors rooted at the scan path."""
    root = Path(scan_root)
    try:
        relative = file_path.relative_to(root)
    except ValueError as exc:
        raise PlanScanPathError(str(file_path), "unsafe_file") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PlanScanPathError(str(file_path), "unsafe_file")

    directory_fds: list[int] = []
    file_fd = -1
    try:
        current_fd = os.open(root, _SCAN_DIRECTORY_FLAGS)
        directory_fds.append(current_fd)
        for component in relative.parts[:-1]:
            current_fd = os.open(component, _SCAN_DIRECTORY_FLAGS, dir_fd=current_fd)
            directory_fds.append(current_fd)
        file_fd = os.open(relative.name, _SCAN_FILE_FLAGS, dir_fd=current_fd)
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = -1
            return handle.read()
    except UnicodeDecodeError:
        raise
    except OSError as exc:
        raise PlanScanPathError(str(file_path), "unsafe_file") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


class PlanIndexer:
    """Scans and indexes plan/spec files, linking them to features."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_svc: GPUEmbeddingService,
        cluster_guard: ClusterGuard,
    ) -> None:
        self._sf = session_factory
        self._embedding_svc = embedding_svc
        self._cluster_guard = cluster_guard

    @staticmethod
    def _canonical_scan_path(scan_path: str) -> str:
        """Validate and canonicalize a scan root before filesystem traversal."""
        candidate = Path(scan_path)
        if not candidate.is_absolute():
            raise PlanScanPathError(scan_path, "relative")

        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PlanScanPathError(scan_path, "missing") from exc

        if not canonical.is_dir():
            raise PlanScanPathError(scan_path, "not_directory")
        if not os.access(canonical, os.R_OK | os.X_OK):
            raise PlanScanPathError(scan_path, "unreadable")
        return str(canonical)

    def parse_plan(self, file_path: str, content: str) -> tuple[str, str]:
        """Extract title and plan_type from file content and path.

        Title priority:
          1. YAML frontmatter ``title:`` or ``name:`` field
          2. First ``# H1`` heading
          3. Filename without date prefix and suffix

        Returns:
            (title, plan_type) where plan_type is 'spec' or 'plan'.
        """
        title = self._extract_title(content, file_path)
        plan_type = self._detect_plan_type(file_path)
        return title, plan_type

    async def dedupe_plans(self, project_key: str) -> int:
        """Consolidate pre-existing mirror-path duplicates in indexed_plans.

        Finds rows that share the same ``content_hash`` within the project,
        keeps one (preferring a row whose ``file_path`` still exists on disk,
        otherwise the most recently indexed), and deletes the rest. Chunks
        cascade-delete via the FK.

        This is a one-shot cleanup for plans that were indexed twice from
        mirrored scan paths (e.g. a primary repo + a monorepo mirror) before
        the duplicate-by-hash guard in ``_reindex_verdict`` was added.

        Returns:
            Number of plan rows deleted.
        """
        async with self._sf() as session:
            # Pull every (id, file_path, content_hash, indexed_at) row whose
            # content_hash has a duplicate inside the same project. The
            # subquery scopes the duplicate detection to the requested project
            # to keep the result set bounded for the in-memory grouping below.
            dup_hashes = (
                sa.select(indexed_plans.c.content_hash)
                .where(indexed_plans.c.project_key == project_key)
                .group_by(indexed_plans.c.content_hash)
                .having(sa.func.count() > 1)
            )
            rows = (
                await session.execute(
                    sa.select(
                        indexed_plans.c.id,
                        indexed_plans.c.file_path,
                        indexed_plans.c.content_hash,
                        indexed_plans.c.indexed_at,
                    )
                    .where(indexed_plans.c.project_key == project_key)
                    .where(indexed_plans.c.content_hash.in_(dup_hashes))
                    .order_by(
                        indexed_plans.c.content_hash,
                        indexed_plans.c.indexed_at.desc(),
                    )
                )
            ).fetchall()

            if not rows:
                return 0

            groups: dict[str, list[Any]] = {}
            for r in rows:
                groups.setdefault(r.content_hash, []).append(r)

            to_delete: list[Any] = []
            for group in groups.values():
                # Prefer a winner whose file still exists on disk; fall back
                # to the first row (already sorted by indexed_at DESC).
                winner = next(
                    (
                        r
                        for r in group
                        if isinstance(r.file_path, str) and Path(r.file_path).is_file()
                    ),
                    group[0],
                )
                to_delete.extend(r.id for r in group if r.id != winner.id)

            if not to_delete:
                return 0

            await session.execute(indexed_plans.delete().where(indexed_plans.c.id.in_(to_delete)))
            await session.commit()

        logger.info(
            "plan_indexer.deduped_plans",
            project_key=project_key,
            deleted=len(to_delete),
        )
        return len(to_delete)

    async def index_path(self, scan_path: str, project_key: str) -> dict[str, Any]:
        """Scan dir for plan files, index new/changed ones concurrently.

        Returns:
            Indexing counters, including ``errors`` when at least one file fails,
            and ``failures`` — the list of ``{file_path, error_type}`` that the
            counter alone cannot name. A count says something is missing; it never
            says WHAT, and the caller would have to leave the tool and grep the
            server journal to find out.

            ``failures`` carries the error TYPE and the path, never the offending
            value: the non-disclosure contract of ``plan_indexer.file_error`` holds
            on the way up too.
        """
        scan_path = self._canonical_scan_path(scan_path)
        stats: dict[str, Any] = {
            "indexed": 0,
            "skipped": 0,
            "linked": 0,
            "chunks_created": 0,
            "errors": 0,
            "failures": [],
        }

        files = self._find_plan_files(scan_path)
        if not files:
            return stats

        sem = asyncio.Semaphore(5)

        async def _process_file(file_path: Path) -> tuple[int, int, int, int, int]:
            """Process one file. Returns (indexed, skipped, linked, errors, chunks_created)."""
            async with sem:
                try:
                    content = _read_scan_file(scan_path, file_path)
                except UnicodeDecodeError:
                    logger.warning("plan_indexer.decode_failed", file=str(file_path))
                    return (0, 0, 0, 1, 0)
                content_hash = self._content_hash(content)

                verdict = await self._reindex_verdict(str(file_path), content_hash, project_key)
                if verdict.skip:
                    return (0, 1, 0, 0, 0)

                # Chunk the markdown content
                parent, chunks = chunk_markdown(content)

                # Resolve title: prefer chunker result, fall back to existing helper
                title = parent.title.strip()[:500] if parent.title.strip() else ""
                if not title:
                    title = self._extract_title(content, str(file_path))[:500]

                plan_type = self._detect_plan_type(str(file_path))

                # Build embed inputs: parent first, then each chunk. Both go
                # through the shared composers in `embedding_text`, so the
                # drift checker recomposes them with the SAME code -- a checker
                # composing its own way reports drift on a column nothing
                # touched. Only the embedding is truncated; the DB keeps the
                # full content.
                embed_inputs = [
                    truncate_plan_embed_input(
                        indexed_plan_embedding_text(title, parent.summary, parent.preamble)
                    )
                ] + [
                    truncate_plan_embed_input(indexed_plan_chunk_embedding_text(c.content))
                    for c in chunks
                ]

                try:
                    all_embeddings = await _embed_in_batches(self._embedding_svc, embed_inputs)
                except Exception:
                    logger.warning("plan_indexer.embed_failed", file=str(file_path), exc_info=True)
                    return (0, 0, 0, 1, 0)

                parent_embedding = all_embeddings[0]
                chunk_embeddings = all_embeddings[1:]

                # Build Pydantic create models
                plan_create = IndexedPlanCreate(
                    file_path=str(file_path),
                    title=title,
                    plan_type=plan_type,  # type: ignore[arg-type]
                    project_key=project_key,
                    content_hash=content_hash,
                    content=parent.content,
                    summary=parent.summary,
                    status=parent.status,  # type: ignore[arg-type]
                    tags=parent.tags,
                    metadata={"source_file": str(file_path)},
                    chunk_count=len(chunks),
                    word_count=parent.word_count,
                )

                chunk_creates = [
                    IndexedPlanChunkCreate(
                        section_title=c.section_title,
                        section_path=c.section_path,
                        content=c.content,
                        section_order=c.section_order,
                        word_count=c.word_count,
                        project_key=project_key,
                        plan_type=plan_type,  # type: ignore[arg-type]
                        status=parent.status,  # type: ignore[arg-type]
                        tags=parent.tags,
                    )
                    for c in chunks
                ]

                # Upsert plan + chunks via repo
                async with self._sf() as session:
                    repo = PgIndexedPlanRepo(session)
                    plan_id = await repo.upsert_plan_with_chunks(
                        plan=plan_create,
                        plan_embedding=parent_embedding,
                        chunks=chunk_creates,
                        chunk_embeddings=chunk_embeddings,
                    )

                linked = 0
                if verdict.content_identical:
                    # Provider refresh: the file is byte-identical to what was
                    # indexed, so this is a new vector for an old signal, not a
                    # new signal. Reopening the feature assignment here would let
                    # a provider switch create and merge features for files
                    # nobody edited -- and `signal_type="plan"` is in
                    # CREATING_SIGNALS, so link-only mode would not stop it.
                    # The existing link is untouched: `_link_plan_to_feature`
                    # inserts with on_conflict_do_nothing and deletes nothing.
                    # `linked` stays 0 because no link was MADE this run; it does
                    # not mean the plan is unlinked.
                    logger.info(
                        "plan_indexer.resolution_skipped",
                        file_path=str(file_path),
                        reason="content_identical",
                    )
                    return (1, 0, 0, 0, len(chunks))
                try:
                    feature, action = await self._cluster_guard.resolve(
                        text=title,
                        embedding=parent_embedding,
                        project_key=project_key,
                        signal_type="plan",
                    )
                    # signal_type="plan" is in CREATING_SIGNALS: link-only
                    # mode never skips it, so resolve() never returns
                    # (None, "skipped") here.
                    assert feature is not None
                    await self._link_plan_to_feature(
                        feature_id=feature.id,  # type: ignore[attr-defined]
                        plan_id=plan_id,
                        similarity_score=1.0 if action == "created" else 0.85,
                    )
                    linked = 1
                except Exception:
                    logger.warning(
                        "plan_indexer.link_failed",
                        file_path=str(file_path),
                        exc_info=True,
                    )

                return (1, 0, linked, 0, len(chunks))

        results = await asyncio.gather(
            *[_process_file(f) for f in files],
            return_exceptions=True,
        )

        for file_path, result in zip(files, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "plan_indexer.file_error",
                    error_type=type(result).__name__,
                    file_path=str(file_path),
                )
                stats["errors"] += 1
                stats["failures"].append(
                    {"file_path": str(file_path), "error_type": type(result).__name__}
                )
                continue
            indexed, skipped, linked, errors, chunks_created = result
            stats["indexed"] += indexed
            stats["skipped"] += skipped
            stats["linked"] += linked
            stats["chunks_created"] += chunks_created
            if errors:
                # A failure INTERNAL to processing the file (decoding,
                # chunking): the path is known, it must be named like the rest.
                stats["errors"] += errors
                stats["failures"].append(
                    {"file_path": str(file_path), "error_type": "PlanFileRejected"}
                )

        logger.info(
            "plan_indexer.index_path_done",
            scan_path=scan_path,
            project_key=project_key,
            **stats,
        )
        return stats

    async def index_project(self, project_key: str) -> dict[str, Any] | None:
        """Index plans for a single project.

        Returns:
            Aggregated stats dict, or None if no paths configured.
        """
        scan_paths = await self._get_scan_paths(project_key)
        if not scan_paths:
            return None
        scan_paths = self._collapse_scan_paths(scan_paths, project_key)

        totals: dict[str, Any] = {
            "indexed": 0,
            "skipped": 0,
            "linked": 0,
            "errors": 0,
            "chunks_created": 0,
            "failures": [],
        }
        for path in scan_paths:
            try:
                stats = await self.index_path(path, project_key)
            except PlanScanPathError as exc:
                logger.warning(
                    "plan_indexer.invalid_scan_path",
                    project_key=project_key,
                    file_path=exc.path,
                    reason_code=exc.reason_code,
                )
                totals["errors"] += 1
                totals["failures"].append(
                    {"file_path": exc.path, "error_type": f"PlanScanPathError:{exc.reason_code}"}
                )
                continue
            totals["failures"].extend(stats.get("failures", []))
            for key in ("indexed", "skipped", "linked", "errors", "chunks_created"):
                totals[key] += stats.get(key, 0)

        return totals

    def _collapse_scan_paths(self, scan_paths: list[str], project_key: str) -> list[str]:
        """Drop the declarations that name a directory another one already names.

        brain-v42 declares its `docs` twice, once through a symlink to itself.
        The traversal canonicalises, so both produce the same file paths and
        the second scan is pure waste -- 55 plans walked twice, every sweep.

        This is the enforcement side of "the configuration stops declaring two
        names for one directory". The other option -- keeping the configured
        name in `file_path` -- cannot work: that column is UNIQUE table-wide,
        so two names for one file means two rows for one plan, which is the
        duplication being repaired.

        A path with no canonical form -- relative, missing, unreadable -- is
        passed through untouched: it cannot be compared to anything, and its
        own error handler is where it must be counted.
        """
        kept: list[str] = []
        seen: set[str] = set()
        for path in scan_paths:
            try:
                canonical = self._canonical_scan_path(path)
            except PlanScanPathError:
                kept.append(path)
                continue
            if canonical in seen:
                logger.info(
                    "plan_indexer.duplicate_scan_path",
                    project_key=project_key,
                    file_path=path,
                    canonical_path=canonical,
                )
                continue
            seen.add(canonical)
            kept.append(path)
        return kept

    async def index_all_projects(self) -> dict[str, dict[str, int]]:
        """Scan all project_contexts with plan_scan_paths configured.

        Returns:
            Dict mapping project_key to stats. Projects with no results
            (None) are excluded.
        """
        project_keys = await self._get_all_project_keys()
        results: dict[str, dict[str, int]] = {}

        for pk in project_keys:
            stats = await self.index_project(pk)
            if stats is not None:
                results[pk] = stats

        return results

    # ── internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _content_hash(content: str) -> str:
        """Compute SHA256 hex digest of content."""
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _find_plan_files(scan_path: str) -> list[Path]:
        """Find all *-design.md and *-plan.md files recursively."""
        root = Path(scan_path)
        if not root.is_dir():
            return []

        design_files = list(root.glob("**/*-design.md"))
        plan_files = list(root.glob("**/*-plan.md"))
        safe_files: list[Path] = []
        for candidate in sorted(set(design_files + plan_files)):
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PlanScanPathError(str(candidate), "unsafe_file") from exc
            if (
                candidate.is_symlink()
                or canonical != candidate
                or not canonical.is_relative_to(root)
                or not canonical.is_file()
            ):
                raise PlanScanPathError(str(candidate), "unsafe_file")
            safe_files.append(canonical)
        return safe_files

    @staticmethod
    def _extract_title(content: str, file_path: str) -> str:
        """Extract title with priority: frontmatter > H1 > filename."""
        # 1. YAML frontmatter title: or name:
        fm_match = _FRONTMATTER_RE.match(content)
        if fm_match:
            fm_block = fm_match.group(1)
            title_match = _FM_TITLE_RE.search(fm_block)
            if title_match:
                return title_match.group(1).strip()
            name_match = _FM_NAME_RE.search(fm_block)
            if name_match:
                return name_match.group(1).strip()

        # 2. First H1 heading
        h1_match = _H1_RE.search(content)
        if h1_match:
            return h1_match.group(1).strip()

        # 3. Filename without date prefix and suffix
        basename = os.path.basename(file_path)
        name = basename.removesuffix(".md")
        # Remove date prefix
        name = _DATE_PREFIX_RE.sub("", name)
        # Remove -design or -plan suffix
        if name.endswith("-design"):
            name = name.removesuffix("-design")
        elif name.endswith("-plan"):
            name = name.removesuffix("-plan")
        return name

    @staticmethod
    def _detect_plan_type(file_path: str) -> str:
        """Detect plan_type from filename suffix: 'spec' or 'plan'."""
        if file_path.endswith(_DESIGN_SUFFIX):
            return "spec"
        return "plan"

    async def _reindex_verdict(
        self, file_path: str, content_hash: str, project_key: str
    ) -> ReindexVerdict:
        """Decide whether to reprocess this file, and whether its text changed.

        The second half of that answer is what lets a provider-driven vector
        refresh be told apart from a new signal. Both reach this method with
        ``skip=False``; only one of them is a reason to reopen a feature
        assignment.

        ``skip=True`` when:
          - the file is already indexed at this exact ``file_path`` with the
            same hash and is not stale, OR
          - another row in the same project shares the same ``content_hash``
            and *its* file still exists on disk (mirror-path duplicate — the
            other path already covers this content, so re-indexing it under a
            different ``file_path`` would just duplicate the row).

        ``skip=False`` when:
          - no row exists for this file_path AND no live duplicate-by-hash
            covers the same content, OR
          - the stored content_hash differs (file edited), OR
          - the stored row is marked stale (migration 014 backfill sets
            ``freshness_status='stale'`` on all pre-chunking rows so they are
            re-chunked on the next reindex run; a provider switch does the same
            to force a re-embed), OR
          - a duplicate-by-hash row exists but its ``file_path`` is gone from
            disk (treat the new file as the canonical replacement).

        ``content_identical=True`` only for the exact-path row whose stored hash
        equals this file's. It is deliberately false for every other branch: a
        new path, an edited file and a replaced duplicate are all new signals.
        """
        async with self._sf() as session:
            # 1. Exact-path lookup (existing behaviour).
            result = await session.execute(
                sa.select(
                    indexed_plans.c.content_hash,
                    indexed_plans.c.freshness_status,
                    indexed_plans.c.id,
                )
                .where(indexed_plans.c.file_path == file_path)
                .where(indexed_plans.c.project_key == project_key)
            )
            row = result.fetchone()

            if row is not None:
                if row.content_hash != content_hash:
                    return ReindexVerdict(skip=False, content_identical=False)
                if row.freshness_status == "stale":
                    return ReindexVerdict(skip=False, content_identical=True)
                return ReindexVerdict(skip=True, content_identical=True)

            # 2. Mirror-path duplicate check: another file in the same project
            # already carries this exact content. Skip iff that file still
            # exists on disk; otherwise treat the new path as a replacement.
            dup_result = await session.execute(
                sa.select(
                    indexed_plans.c.id,
                    indexed_plans.c.file_path,
                )
                .where(indexed_plans.c.content_hash == content_hash)
                .where(indexed_plans.c.project_key == project_key)
                .limit(1)
            )
            dup_row = dup_result.fetchone()

        if dup_row is None:
            return ReindexVerdict(skip=False, content_identical=False)

        existing_path = dup_row.file_path
        if not isinstance(existing_path, str):
            return ReindexVerdict(skip=False, content_identical=False)
        existing_file = Path(existing_path)
        if not existing_file.is_absolute() or not existing_file.is_file():
            return ReindexVerdict(skip=False, content_identical=False)

        logger.info(
            "plan_indexer.duplicate_content_skipped",
            new_path=file_path,
            existing_path=existing_path,
            content_hash=content_hash[:16],
        )
        return ReindexVerdict(skip=True, content_identical=False)

    async def _link_plan_to_feature(
        self,
        feature_id: object,
        plan_id: object,
        similarity_score: float,
    ) -> None:
        """Insert feature_artifact link (plan -> feature)."""
        async with self._sf() as session:
            stmt = (
                pg_insert(feature_artifacts)
                .values(
                    feature_id=feature_id,
                    artifact_type="plan",
                    artifact_id=plan_id,
                    similarity_score=similarity_score,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)
            await session.commit()

    async def _get_scan_paths(self, project_key: str) -> list[str] | None:
        """Get plan_scan_paths from project_contexts for a project_key."""
        async with self._sf() as session:
            stmt = sa.select(project_contexts.c.plan_scan_paths).where(
                project_contexts.c.project_key == project_key
            )
            result = await session.execute(stmt)
            row = result.fetchone()

        if row is None:
            return None

        paths = row.plan_scan_paths
        if not paths:
            return None

        return list(paths)

    async def _get_all_project_keys(self) -> list[str]:
        """Get all project_keys that have plan_scan_paths configured."""
        async with self._sf() as session:
            stmt = sa.select(
                project_contexts.c.project_key,
                project_contexts.c.plan_scan_paths,
            ).where(project_contexts.c.plan_scan_paths.isnot(None))
            result = await session.execute(stmt)
            rows = result.fetchall()

        return [row.project_key for row in rows if row.plan_scan_paths]
