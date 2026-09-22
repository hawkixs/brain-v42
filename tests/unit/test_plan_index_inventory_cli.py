"""The inventory CLI: read-only by default, and never a delete in any mode.

The pure classifier is tested next to the module. What is tested here is
everything the classifier deliberately does not touch: resolving scan roots
through symlinks, walking the disk, and the statements `--apply` issues.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

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
    assert body.index("if not args.apply:") < body.index("await apply_mutations(")


def test_apply_refuses_when_the_recovery_file_already_exists() -> None:
    """Overwriting the previous run's recovery would delete the only way back."""
    source = SCRIPT.read_text()
    assert "if recovery_path.exists():" in source
    assert source.index("if recovery_path.exists():") < source.index("await apply_mutations(")
