from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from headless_agents.context import ContextBundle, resolve_context


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_full_reads_an_ignored_claude_md(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("RULE-IGNORED-7\n", encoding="utf-8")
    bundle = resolve_context(level="full", repository_root=repo)
    assert [f.source.name for f in bundle.repository_files()] == ["CLAUDE.md"]
    assert "RULE-IGNORED-7" in bundle.preamble(include_repository=True)


def test_levels(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_text("repo rule\n", encoding="utf-8")
    user = tmp_path / "user-CLAUDE.md"
    user.write_text("user rule\n", encoding="utf-8")
    full = resolve_context(level="full", repository_root=repo, user_files=[user])
    glob = resolve_context(level="global", repository_root=repo, user_files=[user])
    none = resolve_context(level="none", repository_root=repo, user_files=[user])
    assert {f.scope for f in full.files} == {"repository", "user"}
    assert {f.scope for f in glob.files} == {"user"}
    assert none.files == ()


def test_missing_user_file_is_skipped_not_raised(tmp_path: Path) -> None:
    bundle = resolve_context(
        level="global", repository_root=None, user_files=[tmp_path / "absent.md"]
    )
    assert bundle.files == ()


def test_parents_only_on_request(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("parent rule\n", encoding="utf-8")
    repo = _git_repo(tmp_path / "repo")
    without = resolve_context(level="full", repository_root=repo)
    with_parents = resolve_context(level="full", repository_root=repo, include_parents=True)
    assert without.files == ()
    assert [f.source for f in with_parents.files if f.source.is_relative_to(tmp_path)] == [
        tmp_path / "CLAUDE.md"
    ]


def test_trace_has_size_and_sha256(tmp_path: Path) -> None:
    user = tmp_path / "u.md"
    user.write_bytes("é\n".encode())
    (entry,) = resolve_context(level="global", repository_root=None, user_files=[user]).to_list()
    assert entry == {
        "path": str(user),
        "scope": "user",
        "size_bytes": 3,
        "sha256": hashlib.sha256("é\n".encode()).hexdigest(),
    }


def test_preamble_without_repository_keeps_user_content(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("repo rule\n", encoding="utf-8")
    user = tmp_path / "u.md"
    user.write_text("user rule\n", encoding="utf-8")
    bundle = resolve_context(level="full", repository_root=repo, user_files=[user])
    preamble = bundle.preamble(include_repository=False)
    assert "user rule" in preamble and "repo rule" not in preamble


def test_empty_bundle_has_empty_preamble() -> None:
    assert ContextBundle(level="none", files=()).preamble(include_repository=True) == ""
