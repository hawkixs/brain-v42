"""Unit tests for PlanIndexer — scans and indexes plan/spec files."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from brain_v42.services.plan_indexer import PlanIndexer, PlanScanPathError

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_deps():
    """Create mock dependencies for PlanIndexer."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    # Support both embed() (single) and embed_texts() (batch) — new code prefers batch
    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)
    embedding_svc.embed_texts = AsyncMock(return_value=[[0.1] * 1536])

    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id=uuid.uuid4(), name="Test Feature")
    cluster_guard.resolve = AsyncMock(return_value=(mock_feature, "linked"))

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "cluster_guard": cluster_guard,
        "mock_feature": mock_feature,
    }


def _build_indexer(deps: dict) -> PlanIndexer:
    return PlanIndexer(
        session_factory=deps["session_factory"],
        embedding_svc=deps["embedding_svc"],
        cluster_guard=deps["cluster_guard"],
    )


# ── scan-path boundary tests ───────────────────────────────────────────


def _make_symlink_loop(tmp_path: Path) -> Path:
    first = tmp_path / "scan-loop-a"
    second = tmp_path / "scan-loop-b"
    first.symlink_to(second.name)
    second.symlink_to(first.name)
    return first


def test_canonical_scan_path_rejects_relative_path(mock_deps):
    """Removing the absolute-path gate must make this test fail."""
    indexer = _build_indexer(mock_deps)

    with pytest.raises(PlanScanPathError) as exc:
        indexer._canonical_scan_path("docs/plans")

    assert exc.value.reason_code == "relative"


def test_canonical_scan_path_rejects_missing_path(mock_deps, tmp_path):
    """Accepting a missing scan root must make this test fail."""
    indexer = _build_indexer(mock_deps)

    with pytest.raises(PlanScanPathError) as exc:
        indexer._canonical_scan_path(str(tmp_path / "missing"))

    assert exc.value.reason_code == "missing"


def test_canonical_scan_path_rejects_regular_file(mock_deps, tmp_path):
    """Accepting a regular file as a scan root must make this test fail."""
    scan_file = tmp_path / "not-a-directory"
    scan_file.write_text("not a directory")
    indexer = _build_indexer(mock_deps)

    with pytest.raises(PlanScanPathError) as exc:
        indexer._canonical_scan_path(str(scan_file))

    assert exc.value.reason_code == "not_directory"


def test_canonical_scan_path_rejects_unreadable_directory(mock_deps, tmp_path):
    """Dropping the read/search permission gate must make this test fail."""
    indexer = _build_indexer(mock_deps)

    with patch("brain_v42.services.plan_indexer.os.access", return_value=False):
        with pytest.raises(PlanScanPathError) as exc:
            indexer._canonical_scan_path(str(tmp_path))

    assert exc.value.reason_code == "unreadable"


def test_canonical_scan_path_rejects_symlink_loop(mock_deps, tmp_path):
    """Letting ``RuntimeError`` escape a symlink loop must make this test fail."""
    scan_path = _make_symlink_loop(tmp_path)
    indexer = _build_indexer(mock_deps)

    with pytest.raises(PlanScanPathError) as exc:
        indexer._canonical_scan_path(str(scan_path))

    assert exc.value.path == str(scan_path)
    assert exc.value.reason_code == "missing"


def test_find_plan_files_rejects_symlinked_plan_outside_root(mock_deps, tmp_path):
    scan_root = tmp_path / "plans"
    scan_root.mkdir()
    outside = tmp_path / "outside-plan.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (scan_root / "escape-plan.md").symlink_to(outside)
    indexer = _build_indexer(mock_deps)

    with pytest.raises(PlanScanPathError) as exc:
        indexer._find_plan_files(str(scan_root.resolve()))

    assert exc.value.reason_code == "unsafe_file"


# ── parse_plan tests ────────────────────────────────────────────────────


def test_parse_plan_extracts_title_from_h1(mock_deps):
    content = "# My Feature Design\n\nSome content here"
    indexer = _build_indexer(mock_deps)
    title, plan_type = indexer.parse_plan("/docs/specs/2026-03-14-feature-design.md", content)
    assert title == "My Feature Design"
    assert plan_type == "spec"


def test_parse_plan_extracts_title_from_frontmatter(mock_deps):
    content = "---\ntitle: Cool Feature\n---\n\n# Implementation\n\nContent"
    indexer = _build_indexer(mock_deps)
    title, plan_type = indexer.parse_plan("/docs/plans/2026-03-14-cool-plan.md", content)
    assert title == "Cool Feature"
    assert plan_type == "plan"


def test_parse_plan_frontmatter_name_field(mock_deps):
    content = "---\nname: Awesome Widget\n---\n\nSome body"
    indexer = _build_indexer(mock_deps)
    title, plan_type = indexer.parse_plan("/docs/specs/awesome-widget-design.md", content)
    assert title == "Awesome Widget"
    assert plan_type == "spec"


def test_parse_plan_falls_back_to_filename(mock_deps):
    content = "Just some content without headings"
    indexer = _build_indexer(mock_deps)
    title, plan_type = indexer.parse_plan("/docs/specs/2026-03-14-my-thing-design.md", content)
    assert title == "my-thing"
    assert plan_type == "spec"


def test_parse_plan_filename_without_date_prefix(mock_deps):
    content = "No headings here"
    indexer = _build_indexer(mock_deps)
    title, plan_type = indexer.parse_plan("/docs/plans/cool-feature-plan.md", content)
    assert title == "cool-feature"
    assert plan_type == "plan"


def test_parse_plan_detects_spec_type_from_design_suffix(mock_deps):
    content = "# Auth Design\n\nDetails"
    indexer = _build_indexer(mock_deps)
    _, plan_type = indexer.parse_plan("/somewhere/auth-design.md", content)
    assert plan_type == "spec"


def test_parse_plan_detects_plan_type_from_plan_suffix(mock_deps):
    content = "# Migration Plan\n\nDetails"
    indexer = _build_indexer(mock_deps)
    _, plan_type = indexer.parse_plan("/somewhere/migration-plan.md", content)
    assert plan_type == "plan"


def test_parse_plan_frontmatter_title_takes_priority_over_h1(mock_deps):
    content = "---\ntitle: FM Title\n---\n\n# H1 Title\n\nBody"
    indexer = _build_indexer(mock_deps)
    title, _ = indexer.parse_plan("/docs/specs/whatever-design.md", content)
    assert title == "FM Title"


# ── index_path tests ────────────────────────────────────────────────────


def _mock_execute_for_new_file(mock_deps):
    """Set up mock session.execute to handle the full index flow for new files.

    The new flow:
      1. _is_unchanged: SELECT by file_path → fetchone() returns None (new file)
      2. _is_unchanged: SELECT by content_hash → fetchone() returns None
         (no mirror-path duplicate)
      3. PgIndexedPlanRepo.upsert_plan_with_chunks (patched separately)
      4. _link_plan_to_feature: INSERT on_conflict_do_nothing

    Returns the plan_id used by the upsert mock.
    """
    plan_id = uuid.uuid4()

    # Result for _is_unchanged step 1 (no existing row at this path)
    unchanged_result = MagicMock()
    unchanged_result.fetchone.return_value = None

    # Result for _is_unchanged step 2 (no duplicate-by-hash row)
    no_dup_result = MagicMock()
    no_dup_result.fetchone.return_value = None

    # Result for _link_plan_to_feature (no return value needed)
    link_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(
        side_effect=[unchanged_result, no_dup_result, link_result]
    )
    mock_deps["_plan_id"] = plan_id
    return plan_id


def _patch_repo_upsert(mock_deps):
    """Return a context manager that patches PgIndexedPlanRepo.upsert_plan_with_chunks."""
    plan_id = mock_deps.get("_plan_id") or uuid.uuid4()
    return patch(
        "brain_v42.services.plan_indexer.PgIndexedPlanRepo.upsert_plan_with_chunks",
        new_callable=AsyncMock,
        return_value=plan_id,
    )


@pytest.mark.asyncio
async def test_index_path_indexes_new_files(mock_deps, tmp_path):
    """New files (no existing hash in DB) should be indexed."""
    spec_file = tmp_path / "2026-03-14-auth-design.md"
    spec_file.write_text("# Auth Design\n\nImplementation details")

    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] >= 1
    assert stats["skipped"] == 0
    # Embedding service should have been called (embed_texts is preferred)
    mock_deps["embedding_svc"].embed_texts.assert_awaited()


@pytest.mark.asyncio
async def test_index_path_persists_canonical_file_path(mock_deps, tmp_path):
    """Removing root canonicalization must preserve a non-canonical ``..`` path and fail."""
    scan_root = tmp_path / "plans"
    scan_root.mkdir()
    plan_file = scan_root / "canonical-design.md"
    plan_file.write_text("# Canonical Design\n\nDetails")
    non_canonical_root = scan_root / ".." / "plans"
    expected_path = str(plan_file.resolve())

    _mock_execute_for_new_file(mock_deps)
    indexer = _build_indexer(mock_deps)

    with _patch_repo_upsert(mock_deps) as upsert:
        stats = await indexer.index_path(str(non_canonical_root), "brain_v42")

    assert stats["indexed"] == 1
    persisted = upsert.await_args.kwargs["plan"]
    assert persisted.file_path == expected_path
    assert persisted.metadata["source_file"] == expected_path


@pytest.mark.asyncio
async def test_index_path_indexes_planned_frontmatter_as_active(mock_deps, tmp_path):
    plan_file = tmp_path / "future-work-plan.md"
    plan_file.write_text(
        """---
title: Future Work
status: planned
---

# Future Work

## Scope

Implementation details.
"""
    )
    _mock_execute_for_new_file(mock_deps)
    mock_deps["embedding_svc"].embed_texts = AsyncMock(return_value=[[0.1] * 1536, [0.1] * 1536])

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps) as upsert:
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 1
    assert stats.get("errors", 0) == 0
    assert upsert.await_args.kwargs["plan"].status == "active"
    chunks = upsert.await_args.kwargs["chunks"]
    assert len(chunks) == 1
    assert chunks[0].status == "active"
    assert len(upsert.await_args.kwargs["chunk_embeddings"]) == 1


@pytest.mark.asyncio
async def test_index_path_rejects_unknown_frontmatter_status(mock_deps, tmp_path):
    plan_file = tmp_path / "unknown-status-plan.md"
    sentinel = "SENSITIVE_PLAN_STATUS_SENTINEL"
    plan_file.write_text(
        f"""---
title: Unknown Status
status: future-{sentinel}
---

# Unknown Status
"""
    )
    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with capture_logs() as logs:
        with _patch_repo_upsert(mock_deps) as upsert:
            stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 0
    assert stats["errors"] == 1
    upsert.assert_not_awaited()
    file_errors = [log for log in logs if log["event"] == "plan_indexer.file_error"]
    assert len(file_errors) == 1
    assert file_errors[0]["error_type"] == "ValidationError"
    assert file_errors[0]["file_path"] == str(plan_file)
    assert sentinel not in repr(logs)


@pytest.mark.parametrize("frontmatter_value", ["false", "0", "[]", "null", "''"])
@pytest.mark.asyncio
async def test_index_path_rejects_explicit_falsy_frontmatter_status(
    mock_deps, tmp_path, frontmatter_value
):
    plan_file = tmp_path / "falsy-status-plan.md"
    plan_file.write_text(
        f"""---
title: Falsy Status
status: {frontmatter_value}
---

# Falsy Status
"""
    )
    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps) as upsert:
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 0
    assert stats["errors"] == 1
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_path_skips_unchanged_files(mock_deps, tmp_path):
    """Files with matching content_hash should be skipped."""
    spec_file = tmp_path / "2026-03-14-auth-design.md"
    content = "# Auth Design\n\nImplementation details"
    spec_file.write_text(content)

    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # DB returns existing row with matching hash
    existing_row = MagicMock()
    existing_row.content_hash = content_hash
    existing_row.id = uuid.uuid4()
    result_set = MagicMock()
    result_set.fetchone.return_value = existing_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 0
    assert stats["skipped"] >= 1
    # Embedding service should NOT have been called for skipped files
    mock_deps["embedding_svc"].embed.assert_not_awaited()
    mock_deps["embedding_svc"].embed_texts.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_path_skips_duplicate_content_at_different_path(mock_deps, tmp_path):
    """Two files with identical content but different paths (mirror dirs)
    must result in one indexed row, not two.

    Reproduces the brain_v42 issue where the same plan was indexed twice from
    a primary repo path and a ReD_v1 monorepo mirror path.
    """
    content = "# Auth Design\n\nIdentical content"
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Existing primary file lives outside the dir we are about to scan but is
    # already in the DB and still exists on disk.
    primary_dir = tmp_path / "primary"
    primary_dir.mkdir()
    primary_file = primary_dir / "auth-design.md"
    primary_file.write_text(content)

    # Mirror file in the dir we'll scan now.
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    mirror_file = mirror_dir / "auth-design.md"
    mirror_file.write_text(content)

    # First SELECT (by file_path = mirror) → no row
    no_row = MagicMock()
    no_row.fetchone.return_value = None

    # Second SELECT (by content_hash + project_key) → existing primary row
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.file_path = str(primary_file)
    existing.content_hash = content_hash
    existing.freshness_status = "fresh"
    by_hash = MagicMock()
    by_hash.fetchone.return_value = existing

    mock_deps["session"].execute = AsyncMock(side_effect=[no_row, by_hash])

    indexer = _build_indexer(mock_deps)
    stats = await indexer.index_path(str(mirror_dir), "brain_v42")

    assert stats["indexed"] == 0, "duplicate-by-hash should NOT create a new row"
    assert stats["skipped"] >= 1
    mock_deps["embedding_svc"].embed_texts.assert_not_awaited()
    mock_deps["embedding_svc"].embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_path_indexes_when_existing_duplicate_file_gone(mock_deps, tmp_path):
    """If the DB has a row pointing to a file that no longer exists on disk,
    a new file with the same content under a different path should be indexed
    (treat the new path as the canonical replacement, not a duplicate).
    """
    content = "# Auth Design\n\nIdentical content"
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    new_dir = tmp_path / "new_location"
    new_dir.mkdir()
    new_file = new_dir / "auth-design.md"
    new_file.write_text(content)

    # First SELECT (by file_path = new) → no row
    no_row = MagicMock()
    no_row.fetchone.return_value = None

    # Second SELECT (by content_hash) → row pointing to vanished path
    stale = MagicMock()
    stale.id = uuid.uuid4()
    stale.file_path = "/no/such/file/anywhere.md"
    stale.content_hash = content_hash
    stale.freshness_status = "fresh"
    by_hash = MagicMock()
    by_hash.fetchone.return_value = stale

    # _link_plan_to_feature
    link_result = MagicMock()
    mock_deps["session"].execute = AsyncMock(side_effect=[no_row, by_hash, link_result])

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(new_dir), "brain_v42")

    assert stats["indexed"] >= 1, (
        "vanished-file duplicate should NOT block re-indexing under the new path"
    )


@pytest.mark.asyncio
async def test_is_unchanged_exact_path_lookup_is_project_scoped(mock_deps, tmp_path):
    """Removing project ownership from the exact lookup must make this test fail."""
    exact_result = MagicMock()
    exact_result.fetchone.return_value = None
    duplicate_result = MagicMock()
    duplicate_result.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(side_effect=[exact_result, duplicate_result])
    indexer = _build_indexer(mock_deps)

    unchanged = await indexer._is_unchanged(
        str(tmp_path / "owned-design.md"),
        "content-hash",
        "red-phone",
    )

    assert unchanged is False
    exact_stmt = mock_deps["session"].execute.await_args_list[0].args[0]
    exact_sql = str(
        exact_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "indexed_plans.file_path =" in exact_sql
    assert "indexed_plans.project_key = 'red-phone'" in exact_sql


@pytest.mark.asyncio
async def test_is_unchanged_ignores_live_relative_duplicate(mock_deps, tmp_path, monkeypatch):
    """Treating a live relative legacy row as canonical must make this test fail."""
    primary_dir = tmp_path / "legacy"
    primary_dir.mkdir()
    relative_file = Path("legacy") / "owned-design.md"
    (tmp_path / relative_file).write_text("# Owned Design")
    monkeypatch.chdir(tmp_path)

    exact_result = MagicMock()
    exact_result.fetchone.return_value = None
    duplicate_row = MagicMock()
    duplicate_row.file_path = str(relative_file)
    duplicate_result = MagicMock()
    duplicate_result.fetchone.return_value = duplicate_row
    mock_deps["session"].execute = AsyncMock(side_effect=[exact_result, duplicate_result])
    indexer = _build_indexer(mock_deps)

    unchanged = await indexer._is_unchanged(
        str((tmp_path / "canonical" / "owned-design.md").resolve()),
        "content-hash",
        "red-phone",
    )

    assert unchanged is False


@pytest.mark.asyncio
async def test_index_path_reindexes_changed_files(mock_deps, tmp_path):
    """Files with different content_hash should be re-indexed."""
    spec_file = tmp_path / "2026-03-14-auth-design.md"
    spec_file.write_text("# Auth Design v2\n\nUpdated content")

    # _is_unchanged returns row with OLD hash (doesn't match)
    existing_row = MagicMock()
    existing_row.content_hash = "old_hash_that_doesnt_match"
    existing_row.id = uuid.uuid4()
    unchanged_result = MagicMock()
    unchanged_result.fetchone.return_value = existing_row

    # _link_plan_to_feature
    link_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(side_effect=[unchanged_result, link_result])

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] >= 1
    assert stats["skipped"] == 0
    mock_deps["embedding_svc"].embed_texts.assert_awaited()


@pytest.mark.asyncio
async def test_index_path_calls_cluster_guard(mock_deps, tmp_path):
    """After indexing, ClusterGuard.resolve() is called to link to features."""
    spec_file = tmp_path / "2026-03-14-auth-design.md"
    spec_file.write_text("# Auth Design\n\nDetails")

    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    mock_deps["cluster_guard"].resolve.assert_awaited()
    assert stats["linked"] >= 1


@pytest.mark.asyncio
async def test_index_path_matches_design_glob(mock_deps, tmp_path):
    """Only *-design.md and *-plan.md files are matched."""
    # Create matching files
    (tmp_path / "auth-design.md").write_text("# Auth Design\nContent")
    (tmp_path / "deploy-plan.md").write_text("# Deploy Plan\nContent")
    # Create non-matching files
    (tmp_path / "readme.md").write_text("# README\nNot a plan")
    (tmp_path / "notes.txt").write_text("notes")

    # 2 files, each needs: _is_unchanged (None → new) + _link_plan_to_feature
    side_effects = []
    for _ in range(2):
        unchanged = MagicMock()
        unchanged.fetchone.return_value = None
        link = MagicMock()
        side_effects.extend([unchanged, link])

    mock_deps["session"].execute = AsyncMock(side_effect=side_effects)
    # embed_texts must return N embeddings (parent + chunks) per call
    mock_deps["embedding_svc"].embed_texts = AsyncMock(return_value=[[0.1] * 1536])

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 2  # only auth-design.md and deploy-plan.md


@pytest.mark.asyncio
async def test_index_path_handles_nested_dirs(mock_deps, tmp_path):
    """Glob should match files in nested directories."""
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "nested-design.md").write_text("# Nested Design\nContent")

    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["indexed"] == 1


@pytest.mark.asyncio
async def test_index_path_empty_dir(mock_deps, tmp_path):
    """Empty directory returns all zeros."""
    indexer = _build_indexer(mock_deps)
    stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats == {
        "indexed": 0,
        "skipped": 0,
        "linked": 0,
        "chunks_created": 0,
        "errors": 0,
        "failures": [],
    }


# ── index_project tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_project_returns_none_when_no_paths(mock_deps):
    """Returns None if no plan_scan_paths configured."""
    # DB returns project_context with no plan_scan_paths
    ctx_row = MagicMock()
    ctx_row.plan_scan_paths = None
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    result = await indexer.index_project("brain_v42")

    assert result is None


@pytest.mark.asyncio
async def test_index_project_returns_none_when_empty_paths(mock_deps):
    """Returns None if plan_scan_paths is empty list."""
    ctx_row = MagicMock()
    ctx_row.plan_scan_paths = []
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    result = await indexer.index_project("brain_v42")

    assert result is None


@pytest.mark.asyncio
async def test_index_project_returns_none_when_project_not_found(mock_deps):
    """Returns None if project_key does not exist."""
    result_set = MagicMock()
    result_set.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    result = await indexer.index_project("nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_index_project_calls_index_path(mock_deps, tmp_path):
    """Calls index_path for each configured scan path."""
    ctx_row = MagicMock()
    ctx_row.plan_scan_paths = [str(tmp_path)]
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    with patch.object(
        indexer,
        "index_path",
        new_callable=AsyncMock,
        return_value={
            "indexed": 1,
            "skipped": 0,
            "linked": 1,
            "errors": 2,
            "chunks_created": 3,
        },
    ) as mock_index:
        result = await indexer.index_project("brain_v42")

    mock_index.assert_awaited_once_with(str(tmp_path), "brain_v42")
    assert result is not None
    assert result["indexed"] == 1
    assert result["errors"] == 2
    assert result["chunks_created"] == 3


@pytest.mark.asyncio
async def test_index_project_counts_invalid_path_without_leaking_exception(mock_deps):
    """Losing typed per-path handling must hide the error count or leak exception text."""
    ctx_row = MagicMock()
    ctx_row.plan_scan_paths = ["/safe/missing"]
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    indexer = _build_indexer(mock_deps)
    sentinel = "SENSITIVE_PATH_EXCEPTION_SENTINEL"

    with patch(
        "brain_v42.services.plan_indexer.Path.resolve",
        side_effect=OSError(sentinel),
    ):
        with capture_logs() as logs:
            result = await indexer.index_project("red-phone")

    assert result == {
        "indexed": 0,
        "skipped": 0,
        "linked": 0,
        "errors": 1,
        "chunks_created": 0,
        "failures": [{"file_path": "/safe/missing", "error_type": "PlanScanPathError:missing"}],
    }
    # Le chemin et le CODE de raison remontent — jamais le texte de l'exception.
    # Cette assertion étend au résultat RENDU la garantie que le test ne vérifiait
    # jusqu'ici que sur le journal.
    assert sentinel not in repr(result)
    invalid_logs = [log for log in logs if log["event"] == "plan_indexer.invalid_scan_path"]
    assert invalid_logs == [
        {
            "event": "plan_indexer.invalid_scan_path",
            "log_level": "warning",
            "project_key": "red-phone",
            "file_path": "/safe/missing",
            "reason_code": "missing",
        }
    ]
    assert sentinel not in repr(logs)


@pytest.mark.asyncio
async def test_index_project_counts_symlink_loop_without_scanning(mock_deps, tmp_path):
    """A symlink loop must be counted and logged before filesystem traversal."""
    scan_path = _make_symlink_loop(tmp_path)
    ctx_row = MagicMock(plan_scan_paths=[str(scan_path)])
    result_set = MagicMock()
    result_set.fetchone.return_value = ctx_row
    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    indexer = _build_indexer(mock_deps)

    with patch.object(
        indexer,
        "_find_plan_files",
        wraps=indexer._find_plan_files,
    ) as find_plan_files:
        with capture_logs() as logs:
            result = await indexer.index_project("red-phone")

    assert result == {
        "indexed": 0,
        "skipped": 0,
        "linked": 0,
        "errors": 1,
        "chunks_created": 0,
        "failures": result["failures"],
    }
    # Le chemin est celui du décor temporaire ; ce qui est épinglé ici est la
    # FORME — un échec nommé, avec son code de raison borné.
    assert [f["error_type"] for f in result["failures"]] == ["PlanScanPathError:missing"]
    assert result["failures"][0]["file_path"].endswith("scan-loop-a")
    find_plan_files.assert_not_called()
    invalid_logs = [log for log in logs if log["event"] == "plan_indexer.invalid_scan_path"]
    assert invalid_logs == [
        {
            "event": "plan_indexer.invalid_scan_path",
            "log_level": "warning",
            "project_key": "red-phone",
            "file_path": str(scan_path),
            "reason_code": "missing",
        }
    ]


# ── index_all_projects tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_all_projects_scans_all(mock_deps):
    """Iterates over all project_contexts with plan_scan_paths."""
    row1 = MagicMock()
    row1.project_key = "proj_a"
    row1.plan_scan_paths = ["/some/path"]
    row2 = MagicMock()
    row2.project_key = "proj_b"
    row2.plan_scan_paths = ["/other/path"]

    result_set = MagicMock()
    result_set.fetchall.return_value = [row1, row2]
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    with patch.object(
        indexer,
        "index_project",
        new_callable=AsyncMock,
        return_value={"indexed": 2, "skipped": 0, "linked": 1, "chunks_created": 4},
    ) as mock_ip:
        results = await indexer.index_all_projects()

    assert "proj_a" in results
    assert "proj_b" in results
    assert mock_ip.await_count == 2


@pytest.mark.asyncio
async def test_index_all_projects_skips_none_results(mock_deps):
    """Projects that return None from index_project are excluded."""
    row1 = MagicMock()
    row1.project_key = "proj_a"
    row1.plan_scan_paths = ["/some/path"]

    result_set = MagicMock()
    result_set.fetchall.return_value = [row1]
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    indexer = _build_indexer(mock_deps)
    with patch.object(
        indexer,
        "index_project",
        new_callable=AsyncMock,
        return_value=None,
    ):
        results = await indexer.index_all_projects()

    assert results == {}


# ── embedding text construction ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_embedding_uses_title_in_parent_embed_input(mock_deps, tmp_path):
    """Parent embed input starts with the plan title."""
    content = "# Auth Design\n\n" + "x" * 1000
    spec_file = tmp_path / "auth-design.md"
    spec_file.write_text(content)

    _mock_execute_for_new_file(mock_deps)

    # Capture embed_texts calls to verify the first input starts with the title
    captured: list[list[str]] = []

    async def _capture_embed_texts(texts: list[str]) -> list[list[float]]:
        captured.append(texts)
        return [[0.1] * 1536 for _ in texts]

    mock_deps["embedding_svc"].embed_texts = _capture_embed_texts

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        await indexer.index_path(str(tmp_path), "brain_v42")

    assert captured, "embed_texts was not called"
    # First element of the first batch is the parent embed input
    parent_input = captured[0][0]
    assert "Auth Design" in parent_input


# ── content hash ────────────────────────────────────────────────────────


def test_content_hash_is_sha256(mock_deps):
    """Content hash should be SHA256 hex digest."""
    indexer = _build_indexer(mock_deps)
    content = "some content"
    expected = hashlib.sha256(content.encode()).hexdigest()
    assert indexer._content_hash(content) == expected


# ── concurrency test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_path_processes_files_concurrently(mock_deps, tmp_path):
    """index_path should use gather() to process files, not sequential loop."""
    import asyncio

    # Create 5 plan files
    for i in range(5):
        (tmp_path / f"2026-01-0{i + 1}-feature-{i}-plan.md").write_text(f"# Plan {i}\nContent")

    # Track concurrency: record how many embed_texts calls are in-flight simultaneously
    in_flight = 0
    max_concurrent = 0

    async def tracking_embed_texts(texts: list[str]) -> list[list[float]]:
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.01)  # small delay to expose concurrency
        in_flight -= 1
        return [[0.1] * 1536 for _ in texts]

    mock_deps["embedding_svc"].embed_texts = tracking_embed_texts

    # DB mock: _is_unchanged returns None (new file), _link_plan_to_feature returns ok
    async def _db_side_effect(*args, **kwargs):
        r = MagicMock()
        r.fetchone.return_value = None  # _is_unchanged sees None → new file
        return r

    mock_deps["session"].execute = _db_side_effect

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "test_project")

    assert stats["indexed"] == 5
    # If parallel: max_concurrent > 1 (multiple embed_texts calls in-flight)
    # If sequential: max_concurrent == 1 (one at a time)
    assert max_concurrent > 1, (
        f"max_concurrent={max_concurrent} — files processed sequentially, not in parallel"
    )


# ── embed failure graceful degradation ─────────────────────────────────


# ── dedupe_plans tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedupe_plans_no_duplicates_returns_zero(mock_deps):
    """When no rows share a content_hash, dedupe is a no-op (no DELETE)."""
    select_result = MagicMock()
    select_result.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(side_effect=[select_result])

    indexer = _build_indexer(mock_deps)
    deleted = await indexer.dedupe_plans("brain_v42")

    assert deleted == 0
    # Only the SELECT should have been issued — no DELETE / no commit.
    assert mock_deps["session"].execute.await_count == 1
    mock_deps["session"].commit.assert_not_called()


@pytest.mark.asyncio
async def test_dedupe_plans_keeps_on_disk_winner(mock_deps, tmp_path):
    """When two rows share content_hash, the row whose file still exists wins."""
    real_file = tmp_path / "primary.md"
    real_file.write_text("# real")

    keeper = MagicMock()
    keeper.id = uuid.uuid4()
    keeper.file_path = str(real_file)
    keeper.content_hash = "hash_X"

    loser = MagicMock()
    loser.id = uuid.uuid4()
    loser.file_path = "/tmp/no/such/file/anywhere.md"
    loser.content_hash = "hash_X"

    select_result = MagicMock()
    select_result.fetchall.return_value = [keeper, loser]
    delete_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(side_effect=[select_result, delete_result])

    indexer = _build_indexer(mock_deps)
    deleted = await indexer.dedupe_plans("brain_v42")

    assert deleted == 1
    assert mock_deps["session"].execute.await_count == 2  # SELECT + DELETE
    mock_deps["session"].commit.assert_awaited()


@pytest.mark.asyncio
async def test_dedupe_plans_falls_back_to_first_when_no_file_on_disk(mock_deps):
    """If neither duplicate's file exists, the first (most recently indexed) wins."""
    first = MagicMock()
    first.id = uuid.uuid4()
    first.file_path = "/tmp/gone/a.md"
    first.content_hash = "hash_Y"

    second = MagicMock()
    second.id = uuid.uuid4()
    second.file_path = "/tmp/gone/b.md"
    second.content_hash = "hash_Y"

    select_result = MagicMock()
    select_result.fetchall.return_value = [first, second]
    delete_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(side_effect=[select_result, delete_result])

    indexer = _build_indexer(mock_deps)
    deleted = await indexer.dedupe_plans("brain_v42")

    # One row removed (the loser), the first stays.
    assert deleted == 1
    mock_deps["session"].commit.assert_awaited()


@pytest.mark.asyncio
async def test_dedupe_plans_handles_three_way_duplicate(mock_deps, tmp_path):
    """Three rows sharing a hash should collapse to one (two deletions)."""
    real_file = tmp_path / "real.md"
    real_file.write_text("# real")

    keeper = MagicMock()
    keeper.id = uuid.uuid4()
    keeper.file_path = str(real_file)
    keeper.content_hash = "hash_Z"

    loser_a = MagicMock()
    loser_a.id = uuid.uuid4()
    loser_a.file_path = "/tmp/missing/a.md"
    loser_a.content_hash = "hash_Z"

    loser_b = MagicMock()
    loser_b.id = uuid.uuid4()
    loser_b.file_path = "/tmp/missing/b.md"
    loser_b.content_hash = "hash_Z"

    select_result = MagicMock()
    select_result.fetchall.return_value = [keeper, loser_a, loser_b]
    delete_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(side_effect=[select_result, delete_result])

    indexer = _build_indexer(mock_deps)
    deleted = await indexer.dedupe_plans("brain_v42")

    assert deleted == 2


@pytest.mark.asyncio
async def test_index_path_continues_when_embed_fails(mock_deps, tmp_path):
    """When embed fails for a file, it is counted as error and loop continues."""
    # Create two plan files
    (tmp_path / "alpha-design.md").write_text("# Alpha Design\nContent A")
    (tmp_path / "beta-design.md").write_text("# Beta Design\nContent B")

    # _is_unchanged returns None (new file) for both
    unchanged_result = MagicMock()
    unchanged_result.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(return_value=unchanged_result)

    # Both embed() and embed_texts() fail
    mock_deps["embedding_svc"].embed = AsyncMock(side_effect=Exception("GPU down"))
    mock_deps["embedding_svc"].embed_texts = AsyncMock(side_effect=Exception("GPU down"))

    indexer = _build_indexer(mock_deps)
    stats = await indexer.index_path(str(tmp_path), "brain_v42")

    # Both files should error, none indexed
    assert stats["indexed"] == 0
    assert stats.get("errors", 0) == 2
    # ClusterGuard should NOT be called
    mock_deps["cluster_guard"].resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Les échecs doivent être NOMMÉS, pas seulement comptés
#
# Ticket 1c6911a4 demande de « confirmer qu'aucun plan attendu n'est
# silencieusement absent de l'index ». `stats["errors"]` est un pur compteur :
# les chemins n'existent que dans structlog, donc dans le journal du serveur.
# Un opérateur qui lit « 3 errors » dans la sortie de brain_reindex_plans ne
# peut pas répondre à la question — il doit quitter le tool et grepper
# journalctl. Le compteur dit qu'il manque quelque chose, jamais quoi.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_path_names_the_files_it_could_not_index(mock_deps, tmp_path):
    sentinel = "SENSITIVE_PLAN_STATUS_SENTINEL"
    plan_file = tmp_path / "unknown-status-plan.md"
    plan_file.write_text(
        f"---\ntitle: Unknown Status\nstatus: future-{sentinel}\n---\n\n# Unknown Status\n"
    )
    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["errors"] == 1
    failures = stats["failures"]
    assert [f["file_path"] for f in failures] == [str(plan_file)]
    assert failures[0]["error_type"] == "ValidationError"

    # NON-DIVULGATION : le type d'erreur et le chemin remontent, JAMAIS la valeur
    # fautive. C'est le contrat que `plan_indexer.file_error` respecte déjà côté
    # journal, et le faire remonter ne doit pas l'affaiblir.
    assert sentinel not in repr(stats)


@pytest.mark.asyncio
async def test_a_clean_run_reports_no_failures_at_all(mock_deps, tmp_path):
    """Le cas nominal ne doit produire aucune liste à lire.

    Sans cette assertion, on « corrigerait » en rendant toujours une clé, et le
    lecteur cesserait de la regarder.
    """
    plan_file = tmp_path / "ok-plan.md"
    plan_file.write_text("---\ntitle: Fine\nstatus: active\n---\n\n# Fine\n")
    _mock_execute_for_new_file(mock_deps)

    indexer = _build_indexer(mock_deps)
    with _patch_repo_upsert(mock_deps):
        stats = await indexer.index_path(str(tmp_path), "brain_v42")

    assert stats["errors"] == 0
    assert stats["failures"] == []
