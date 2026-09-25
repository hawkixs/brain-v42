"""The repository's identity, read from the filesystem without git (plan decision P2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from headless_agents.repo import RepoError, discover


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git("init", "-q", cwd=root)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=root,
    )
    return root


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"discover() started a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_a_plain_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    identity = discover(repo)
    assert identity is not None
    assert identity.work_tree == repo.resolve()
    assert identity.git_dir == (repo / ".git").resolve()
    assert identity.common_dir == (repo / ".git").resolve()


def test_a_linked_worktree_names_its_repository_common_dir(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), cwd=repo)
    identity = discover(linked)
    assert identity is not None
    assert identity.work_tree == linked.resolve()
    assert identity.git_dir != identity.common_dir
    assert identity.common_dir == (repo / ".git").resolve()


def test_a_start_below_the_work_tree_finds_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    identity = discover(deep)
    assert identity is not None and identity.work_tree == repo.resolve()


def test_no_repository_is_none(tmp_path: Path) -> None:
    """Bounded by a ceiling, like GIT_CEILING_DIRECTORIES: the host's /tmp may itself
    sit inside a repository."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert discover(lonely, ceiling=tmp_path) is None


def test_a_repository_at_the_ceiling_is_still_found(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    identity = discover(repo, ceiling=repo)
    assert identity is not None and identity.work_tree == repo.resolve()


def test_a_symlinked_git_dir_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    other = tmp_path / "work"
    other.mkdir()
    (other / ".git").symlink_to(repo / ".git")
    with pytest.raises(RepoError, match="cannot be pinned"):
        discover(other)


def test_a_git_file_naming_nothing_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: /does/not/exist\n")
    with pytest.raises(RepoError, match="cannot be pinned"):
        discover(work)


def test_discover_runs_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), cwd=repo)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"discover() started a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert discover(linked) is not None
