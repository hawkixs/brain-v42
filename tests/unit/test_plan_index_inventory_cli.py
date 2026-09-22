"""The inventory CLI: read-only by default, preserving all knowledge rows.

The pure classifier is tested next to the module. What is tested here is
everything the classifier deliberately does not touch: resolving scan roots
through symlinks, walking the disk, and the statements `--apply` issues.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "plan_index_inventory.py"

#: A port nothing listens on: the connection is refused at once, so the failure
#: path is measured without waiting on a timeout.
UNREACHABLE_DSN = "postgresql://brain@127.0.0.1:1/brain"


def _load_cli():
    spec = importlib.util.spec_from_file_location("plan_index_inventory_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


# ── scan roots: acceptance criterion 3 ──────────────────────────────────


def test_two_names_for_one_directory_collapse_to_one_root(tmp_path: Path) -> None:
    """brain-v42's case, measured: `ReD_v1/projects/brain-v42` is a symlink to
    `git_repo/brain_v42`, and both are declared. Scanning twice is wasted work;
    storing both names would be two rows for one plan, and `file_path` is
    UNIQUE."""
    real = tmp_path / "git_repo" / "brain_v42" / "docs"
    real.mkdir(parents=True)
    alias_parent = tmp_path / "ReD_v1" / "projects"
    alias_parent.mkdir(parents=True)
    (alias_parent / "brain-v42").symlink_to(tmp_path / "git_repo" / "brain_v42")

    roots, duplicates, unresolvable = cli.collapse_scan_roots(
        {"brain-v42": [str(real), str(alias_parent / "brain-v42" / "docs")]}
    )

    assert roots["brain-v42"] == [real.resolve()]
    assert duplicates == [
        (
            "brain-v42",
            str(real.resolve()),
            sorted([str(real), str(alias_parent / "brain-v42" / "docs")]),
        )
    ]
    assert unresolvable == []


def test_a_relative_scan_path_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """Seven projects still carry one. A path that resolves to nothing is the
    reason their plans were never indexed; omitting it from the report is how
    it stayed invisible for two months."""
    roots, _, unresolvable = cli.collapse_scan_roots({"red-quant": ["docs/plans/"]})

    assert roots["red-quant"] == []
    assert unresolvable == [("red-quant", "docs/plans/")]


def test_a_scan_path_pointing_at_a_file_is_unresolvable(tmp_path: Path) -> None:
    target = tmp_path / "a-plan.md"
    target.write_text("# Plan")

    roots, _, unresolvable = cli.collapse_scan_roots({"p": [str(target)]})

    assert roots["p"] == []
    assert unresolvable == [("p", str(target))]


# ── walking the disk ────────────────────────────────────────────────────


def test_only_plan_and_design_files_are_collected(tmp_path: Path) -> None:
    (tmp_path / "a-design.md").write_text("# A")
    (tmp_path / "b-plan.md").write_text("# B")
    (tmp_path / "README.md").write_text("# no")
    (tmp_path / "notes.txt").write_text("no")

    found = cli.walk_disk({"p": [tmp_path]})

    assert [Path(f.path).name for f in found] == ["a-design.md", "b-plan.md"]
    assert found[0].content_hash == hashlib.sha256(b"# A").hexdigest()
    assert found[0].project_key == "p"


def test_a_file_past_the_size_ceiling_is_skipped_not_hashed(tmp_path: Path) -> None:
    """A plan is a markdown document. Reading a huge file to find out otherwise
    is the slow way to learn it."""
    big = tmp_path / "huge-plan.md"
    big.write_bytes(b"x" * (cli.MAX_PLAN_FILE_BYTES + 1))
    (tmp_path / "small-plan.md").write_text("# small")

    found = cli.walk_disk({"p": [tmp_path]})

    assert [Path(f.path).name for f in found] == ["small-plan.md"]


def test_the_same_file_reached_from_two_roots_is_collected_once(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a-plan.md").write_text("# A")
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    found = cli.walk_disk({"p": [real, alias]})

    assert len(found) == 1


def test_a_symlinked_plan_file_is_not_an_eligible_plan(tmp_path: Path) -> None:
    """The runtime never indexes a file reached through a symlink."""
    scan_root = tmp_path / "plans"
    scan_root.mkdir()
    outside = tmp_path / "outside-plan.md"
    outside.write_text("# Outside")
    (scan_root / "inside-plan.md").symlink_to(outside)

    assert cli.walk_disk({"p": [scan_root]}) == []


def test_walk_disk_records_a_directory_walk_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _walk(_root, *, topdown, onerror, followlinks):
        onerror(PermissionError(13, "denied", str(tmp_path / "hidden")))
        return iter(())

    monkeypatch.setattr(cli.os, "walk", _walk)
    issues: list[str] = []

    assert cli.walk_disk({"p": [tmp_path]}, issues) == []
    assert issues == [str(tmp_path / "hidden")]


def test_walk_disk_rejects_one_file_claimed_by_two_projects(tmp_path: Path) -> None:
    """Collection must not discard the second owner before classification."""
    plan = tmp_path / "a-plan.md"
    plan.write_text("# A")

    with pytest.raises(cli.InventoryApplyError, match="cross_project_path_claim"):
        cli.walk_disk({"red-games": [tmp_path], "red-writer": [tmp_path]})


# ── what --apply actually writes ────────────────────────────────────────


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    def transaction(self):
        connection = self

        class _Tx:
            async def __aenter__(self):
                connection.statements.append(("BEGIN", ()))
                return connection

            async def __aexit__(self, *exc):
                connection.statements.append(("COMMIT", ()))
                return False

        return _Tx()

    async def execute(self, sql: str, *args) -> None:
        self.statements.append((" ".join(sql.split()), args))


class _LockedRecordingConnection(_RecordingConnection):
    async def fetch(self, sql: str, *args):
        self.statements.append((" ".join(sql.split()), args))
        if "FROM indexed_plans" in sql:
            return [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "file_path": "docs/a-plan.md",
                    "project_key": "red-games",
                    "content_hash": "a" * 64,
                    "freshness_status": "fresh",
                    "freshness_source": None,
                    "indexed_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        if "FROM indexed_plan_chunks" in sql:
            return [
                {
                    "id": "chunk-1",
                    "plan_id": "11111111-1111-1111-1111-111111111111",
                    "project_key": "red-games",
                }
            ]
        if "FROM feature_artifacts" in sql:
            return [
                {
                    "feature_id": "feature-1",
                    "artifact_type": "plan",
                    "artifact_id": "11111111-1111-1111-1111-111111111111",
                    "similarity_score": 0.9,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "feature_project_key": "red-other",
                }
            ]
        raise AssertionError(sql)


def _mutation(kind: str, row_id: str = "11111111-1111-1111-1111-111111111111"):
    from brain_v42.maintenance.plan_index_inventory import RowMutation

    before = {
        "file_path": "docs/a-plan.md",
        "project_key": "red-games",
        "freshness_status": "fresh",
    }
    after = (
        {"file_path": "/w/a-plan.md", "project_key": "red-writer", "freshness_status": "fresh"}
        if kind == "rewrite"
        else {**before, "freshness_status": "archived"}
    )
    return RowMutation(row_id=row_id, kind=kind, before=before, after=after)  # type: ignore[arg-type]


def _dependents() -> dict[str, list[dict[str, object]]]:
    return {
        "chunks": [
            {
                "id": "chunk-1",
                "plan_id": "11111111-1111-1111-1111-111111111111",
                "project_key": "red-games",
            }
        ],
        "feature_edges": [
            {
                "feature_id": "feature-1",
                "artifact_type": "plan",
                "artifact_id": "11111111-1111-1111-1111-111111111111",
                "similarity_score": 0.9,
                "created_at": "2026-01-01T00:00:00+00:00",
                "feature_project_key": "red-other",
            }
        ],
    }


@pytest.mark.asyncio
async def test_apply_never_issues_a_delete() -> None:
    """The operator rule, pinned at the only place that can break it."""
    conn = _RecordingConnection()

    await cli.apply_mutations(
        conn, (_mutation("rewrite"), _mutation("archive", "2" * 8 + "-2222-2222-2222-222222222222"))
    )

    issued = " ".join(sql for sql, _ in conn.statements).upper()
    assert "DELETE" not in issued
    assert "DROP" not in issued
    assert "TRUNCATE" not in issued


@pytest.mark.asyncio
async def test_a_rewrite_moves_the_chunks_with_the_plan() -> None:
    """Chunks carry their own `project_key` and are searched on it. Leaving them
    behind files a plan under one project and its sections under another."""
    conn = _RecordingConnection()

    await cli.apply_mutations(conn, (_mutation("rewrite"),))

    chunk_updates = [(sql, args) for sql, args in conn.statements if "indexed_plan_chunks" in sql]
    assert len(chunk_updates) == 1
    assert chunk_updates[0][1][1] == "red-writer"


@pytest.mark.asyncio
async def test_every_mutation_is_inside_one_transaction() -> None:
    """A half-applied repair is a state nobody measured and no report describes."""
    conn = _RecordingConnection()

    await cli.apply_mutations(
        conn, (_mutation("rewrite"), _mutation("archive", "3" * 8 + "-3333-3333-3333-333333333333"))
    )

    kinds = [sql for sql, _ in conn.statements]
    assert kinds[0] == "BEGIN"
    assert kinds[-1] == "COMMIT"
    assert kinds.count("BEGIN") == 1


@pytest.mark.asyncio
async def test_an_archive_writes_the_status_and_leaves_the_path(tmp_path: Path) -> None:
    conn = _RecordingConnection()

    await cli.apply_mutations(conn, (_mutation("archive"),))

    updates = [sql for sql, _ in conn.statements if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert "freshness_status = 'archived'" in updates[0]
    assert "file_path" not in updates[0]


@pytest.mark.asyncio
async def test_locked_apply_snapshots_dependents_and_detaches_only_foreign_links(
    tmp_path: Path,
) -> None:
    """Recovery is derived after locks, before the first mutation."""
    conn = _LockedRecordingConnection()
    recovery = tmp_path / "recovery.json"

    written, digest = await cli.apply_with_recovery(
        conn,
        (_mutation("rewrite"),),
        recovery,
        expected_dependents=_dependents(),
        detach_cross_project_links=True,
    )

    assert written == 1
    assert len(digest) == 64
    payload = json.loads(recovery.read_text())
    assert payload["plans"][0]["content_hash"] == "a" * 64
    assert payload["chunks"] == [
        {
            "id": "chunk-1",
            "plan_id": "11111111-1111-1111-1111-111111111111",
            "project_key": "red-games",
        }
    ]
    assert payload["feature_edges"][0]["feature_project_key"] == "red-other"
    statements = " ".join(sql for sql, _ in conn.statements)
    assert "SET LOCAL lock_timeout" in statements
    assert "LOCK TABLE indexed_plan_chunks, feature_artifacts" in statements
    assert statements.count("FOR UPDATE") == 3
    assert "DELETE FROM feature_artifacts" in statements


@pytest.mark.asyncio
async def test_locked_apply_refuses_a_foreign_link_without_explicit_detach(tmp_path: Path) -> None:
    conn = _LockedRecordingConnection()

    with pytest.raises(cli.InventoryApplyError, match="cross_project_feature_link"):
        await cli.apply_with_recovery(
            conn,
            (_mutation("rewrite"),),
            tmp_path / "recovery.json",
            expected_dependents=_dependents(),
            detach_cross_project_links=False,
        )

    assert not (tmp_path / "recovery.json").exists()


@pytest.mark.asyncio
async def test_locked_apply_refuses_a_changed_dependent_baseline(tmp_path: Path) -> None:
    conn = _LockedRecordingConnection()
    expected = {"chunks": [], "feature_edges": []}

    with pytest.raises(cli.InventoryApplyError, match="dependent_state_changed"):
        await cli.apply_with_recovery(
            conn,
            (_mutation("rewrite"),),
            tmp_path / "recovery.json",
            expected_dependents=expected,
            detach_cross_project_links=True,
        )

    assert not (tmp_path / "recovery.json").exists()


def test_locked_row_comparison_refuses_a_missing_observed_field() -> None:
    mutation = _mutation("rewrite")
    mutation.before["content_hash"] = "a" * 64

    assert cli._locked_row_matches({"file_path": "docs/a-plan.md"}, mutation) is False


# ── the process contract ────────────────────────────────────────────────


def test_an_unreachable_database_exits_two_without_a_traceback() -> None:
    """An operator runs this before a window. It must say what is wrong, not
    print a stack and leave them guessing whether the corpus is fine."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--postgres-url", UNREACHABLE_DSN],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
    )

    assert result.returncode == 2
    assert "cannot reach PostgreSQL" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_script_is_read_only_unless_apply_is_passed() -> None:
    """Every write lives behind `--apply`, and the source says so once."""
    source = SCRIPT.read_text()
    body = source.split("async def run(", 1)[1]
    assert "if not args.apply:" in body
    assert body.index("if not args.apply:") < body.index("await apply_with_recovery(")


def test_apply_refuses_when_the_recovery_file_already_exists() -> None:
    """Overwriting the previous run's recovery would delete the only way back."""
    source = SCRIPT.read_text()
    assert "if recovery_path.exists():" in source
    assert source.index("if recovery_path.exists():") < source.index("await apply_with_recovery(")


@pytest.mark.asyncio
async def test_apply_refuses_an_incomplete_scan_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing roots are missing evidence, never proof that a row is orphaned."""

    class _Connection:
        async def close(self) -> None:
            pass

    async def _connect(_dsn: str) -> _Connection:
        return _Connection()

    async def _paths(_conn: _Connection) -> dict[str, list[str]]:
        return {"red-writer": [str(tmp_path), "relative/path"]}

    async def _rows(_conn: _Connection):
        from brain_v42.maintenance.plan_index_inventory import PlanRow

        return [
            PlanRow(
                row_id="1",
                project_key="red-writer",
                file_path="missing-plan.md",
                content_hash="a" * 64,
                freshness_status="fresh",
                indexed_at="2026-01-01T00:00:00+00:00",
            )
        ]

    async def _apply(_conn: _Connection, _mutations: tuple) -> int:
        return 1

    monkeypatch.setattr(cli.asyncpg, "connect", _connect)
    monkeypatch.setattr(cli, "read_scan_paths", _paths)
    monkeypatch.setattr(cli, "read_rows", _rows)
    monkeypatch.setattr(cli, "apply_mutations", _apply)
    args = SimpleNamespace(
        postgres_url="postgresql://safe@localhost/test",
        json=False,
        verify=False,
        apply=True,
        max_mutations=10,
        recovery_file=str(tmp_path / "unused.json"),
    )

    assert await cli.run(args) == 2


@pytest.mark.asyncio
async def test_verify_refuses_an_incomplete_scan_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Connection:
        async def close(self) -> None:
            pass

    async def _connect(_dsn: str) -> _Connection:
        return _Connection()

    async def _paths(_conn: _Connection) -> dict[str, list[str]]:
        return {"red-writer": [str(tmp_path), "relative/path"]}

    async def _rows(_conn: _Connection):
        return []

    monkeypatch.setattr(cli.asyncpg, "connect", _connect)
    monkeypatch.setattr(cli, "read_scan_paths", _paths)
    monkeypatch.setattr(cli, "read_rows", _rows)
    args = SimpleNamespace(
        postgres_url="postgresql://safe@localhost/test",
        json=False,
        verify=True,
        apply=False,
        max_mutations=10,
        recovery_file=str(tmp_path / "unused.json"),
    )

    assert await cli.run(args) == 2


@pytest.mark.asyncio
async def test_run_returns_two_for_cross_project_disk_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "a-plan.md").write_text("# A")

    class _Connection:
        async def close(self) -> None:
            pass

    async def _connect(_dsn: str) -> _Connection:
        return _Connection()

    async def _paths(_conn: _Connection) -> dict[str, list[str]]:
        return {"red-games": [str(tmp_path)], "red-writer": [str(tmp_path)]}

    async def _rows(_conn: _Connection):
        return []

    monkeypatch.setattr(cli.asyncpg, "connect", _connect)
    monkeypatch.setattr(cli, "read_scan_paths", _paths)
    monkeypatch.setattr(cli, "read_rows", _rows)
    args = SimpleNamespace(
        postgres_url="postgresql://safe/test", json=False, verify=False, apply=False
    )

    assert await cli.run(args) == 2


@pytest.mark.asyncio
async def test_apply_refuses_when_an_eligible_file_cannot_be_hashed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An oversized plan is missing evidence, not an orphaning signal."""
    oversized = tmp_path / "too-large-plan.md"
    oversized.write_bytes(b"x" * (cli.MAX_PLAN_FILE_BYTES + 1))

    class _Connection:
        async def close(self) -> None:
            pass

    async def _connect(_dsn: str) -> _Connection:
        return _Connection()

    async def _paths(_conn: _Connection) -> dict[str, list[str]]:
        return {"red-writer": [str(tmp_path)]}

    async def _rows(_conn: _Connection):
        from brain_v42.maintenance.plan_index_inventory import PlanRow

        return [
            PlanRow(
                "1", "red-writer", "missing-plan.md", "a" * 64, "fresh", "2026-01-01T00:00:00+00:00"
            )
        ]

    async def _apply(_conn: _Connection, _mutations: tuple) -> int:
        return 1

    monkeypatch.setattr(cli.asyncpg, "connect", _connect)
    monkeypatch.setattr(cli, "read_scan_paths", _paths)
    monkeypatch.setattr(cli, "read_rows", _rows)
    monkeypatch.setattr(cli, "apply_mutations", _apply)
    args = SimpleNamespace(
        postgres_url="postgresql://safe@localhost/test",
        json=False,
        verify=False,
        apply=True,
        max_mutations=10,
        recovery_file=str(tmp_path / "unused.json"),
    )

    assert await cli.run(args) == 2
