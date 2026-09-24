"""The ``.git`` tripwire and the hardened git invocation (Brain ticket 0b622f47).

In a writable workspace an agent can plant what the NEXT git command runs
outside every sandbox -- a hook, a ``core.fsmonitor``, a rewritten ``.git``
file of a linked worktree, a hook in a tracked ``core.hooksPath`` directory --
and none of it shows in ``git status`` or ``git diff``. The tripwire
fingerprints every such place before the run and compares after; the hardened
invocation makes sure git never discovers a repository the agent built.

Every test runs against a real ``git`` on real throwaway repositories.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from headless_agents import git_tripwire
from headless_agents.git_tripwire import GitTampered, Tripwire, git_command, settle
from headless_agents.profile import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    (path / ".gitconfig").write_text("[user]\n\tname = op\n", encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    return root


@pytest.fixture
def linked(repo: Path, tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "ha/run", str(worktree))
    return worktree


def _arm(root: Path, home: Path) -> Tripwire:
    tripwire = Tripwire.arm(Workspace(path=root, write=True), home=home)
    assert tripwire is not None
    return tripwire


# ── Arming ─────────────────────────────────────────────────────────────────


def test_a_read_only_workspace_is_not_armed(repo: Path, home: Path) -> None:
    assert Tripwire.arm(Workspace(path=repo), home=home) is None
    assert Tripwire.arm(None, home=home) is None


def test_an_untouched_repository_does_not_trip(repo: Path, home: Path) -> None:
    tripwire = _arm(repo, home)
    (repo / "src.py").write_text("print('edited by the agent')\n", encoding="utf-8")
    assert tripwire.tampered() == ()


def test_index_objects_and_refs_do_not_trip(repo: Path, home: Path) -> None:
    """An agent committing with its own shell runs nothing dangerous later."""
    tripwire = _arm(repo, home)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "agent")
    assert tripwire.tampered() == ()


# ── What must trip ─────────────────────────────────────────────────────────


def test_a_planted_hook_trips(repo: Path, home: Path) -> None:
    tripwire = _arm(repo, home)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8")
    hook.chmod(0o755)
    assert str(hook) in tripwire.tampered()


def test_a_mode_change_on_an_existing_hook_trips(repo: Path, home: Path) -> None:
    sample = repo / ".git" / "hooks" / "pre-commit.sample"
    tripwire = _arm(repo, home)
    sample.chmod(0o777)
    assert str(sample) in tripwire.tampered()


def test_an_fsmonitor_in_the_repository_config_trips(repo: Path, home: Path) -> None:
    tripwire = _arm(repo, home)
    _git(repo, "config", "core.fsmonitor", "touch /tmp/pwned")
    assert str(repo / ".git" / "config") in tripwire.tampered()


def test_info_attributes_trip(repo: Path, home: Path) -> None:
    tripwire = _arm(repo, home)
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
    assert str(repo / ".git" / "info" / "attributes") in tripwire.tampered()


def test_a_hooks_directory_swapped_for_a_symlink_trips(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    tripwire = _arm(repo, home)
    elsewhere = tmp_path / "evil-hooks"
    elsewhere.mkdir()
    shutil.rmtree(repo / ".git" / "hooks")
    (repo / ".git" / "hooks").symlink_to(elsewhere)
    assert str(repo / ".git" / "hooks") in tripwire.tampered()


def test_a_git_directory_created_where_there_was_none_trips(tmp_path: Path, home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    tripwire = _arm(plain, home)
    (plain / ".git").mkdir()
    (plain / ".git" / "config").write_text("[core]\n\tfsmonitor = touch /tmp/x\n", encoding="utf-8")
    assert str(plain / ".git") in tripwire.tampered()


def test_the_user_gitconfig_trips(repo: Path, home: Path) -> None:
    tripwire = _arm(repo, home)
    (home / ".gitconfig").write_text("[core]\n\tfsmonitor = touch /tmp/x\n", encoding="utf-8")
    assert str(home / ".gitconfig") in tripwire.tampered()


def test_a_tracked_hooks_path_directory_trips(repo: Path, home: Path) -> None:
    """husky-style: core.hooksPath names a directory INSIDE the work tree."""
    (repo / ".husky").mkdir()
    _git(repo, "config", "core.hooksPath", ".husky")
    tripwire = _arm(repo, home)
    (repo / ".husky" / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8")
    assert str(repo / ".husky" / "pre-commit") in tripwire.tampered()


# ── Linked worktrees ───────────────────────────────────────────────────────


def test_a_rewritten_dot_git_file_trips(linked: Path, home: Path, tmp_path: Path) -> None:
    tripwire = _arm(linked, home)
    fake = linked / ".evil"
    fake.mkdir()
    (linked / ".git").write_text(f"gitdir: {fake}\n", encoding="utf-8")
    assert str(linked / ".git") in tripwire.tampered()


def test_the_common_config_of_a_linked_worktree_trips(linked: Path, repo: Path, home: Path) -> None:
    tripwire = _arm(linked, home)
    _git(repo, "config", "core.fsmonitor", "touch /tmp/pwned")
    assert str(repo / ".git" / "config") in tripwire.tampered()


def test_a_hook_in_the_common_dir_of_a_linked_worktree_trips(
    linked: Path, repo: Path, home: Path
) -> None:
    tripwire = _arm(linked, home)
    (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n", encoding="utf-8")
    assert str(repo / ".git" / "hooks" / "post-checkout") in tripwire.tampered()


def test_the_per_worktree_gitdir_pointer_trips(linked: Path, repo: Path, home: Path) -> None:
    tripwire = _arm(linked, home)
    pointer = repo / ".git" / "worktrees" / "wt" / "commondir"
    pointer.write_text("/somewhere/else\n", encoding="utf-8")
    assert str(pointer) in tripwire.tampered()


# ── Settling a run ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("child_code", [0, 3, 4, 1])
def test_a_tampered_run_is_a_non_replayable_failure(tmp_path: Path, child_code: int) -> None:
    stderr = tmp_path / "stderr.log"
    code, tampered = settle(("/w/.git/hooks/pre-commit",), child_code, stderr)
    assert code == 1
    assert tampered == ("/w/.git/hooks/pre-commit",)
    assert "/w/.git/hooks/pre-commit" in stderr.read_text()


def test_a_clean_run_keeps_its_code(tmp_path: Path) -> None:
    stderr = tmp_path / "stderr.log"
    assert settle((), 0, stderr) == (0, ())
    assert not stderr.exists()


def test_an_unarmed_run_reports_nothing(tmp_path: Path) -> None:
    assert settle(None, 3, tmp_path / "stderr.log") == (3, None)


# ── The hardened git invocation ────────────────────────────────────────────


def test_git_command_pins_the_repository_and_the_work_tree(repo: Path) -> None:
    command = git_command(repo)
    assert command[:3] == ["git", "-C", str(repo)]
    assert command[command.index("--git-dir") + 1] == str(repo / ".git")
    assert command[command.index("--work-tree") + 1] == str(repo)
    assert "safe.bareRepository=explicit" in command
    assert "core.fsmonitor=false" in command


def test_git_command_resolves_a_linked_worktree_git_dir(linked: Path, repo: Path) -> None:
    command = git_command(linked)
    assert command[command.index("--git-dir") + 1] == str(repo / ".git" / "worktrees" / "wt")


def test_git_command_refuses_a_tampered_workspace(repo: Path) -> None:
    with pytest.raises(GitTampered):
        git_command(repo, tampered=("x",))


def test_git_command_refuses_a_dot_git_file_pointing_nowhere(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / ".git").write_text("gitdir: /does/not/exist\n", encoding="utf-8")
    with pytest.raises(GitTampered):
        git_command(root)


def _plant_nested_repository(repo: Path, marker: Path) -> Path:
    """An agent-built repository in a subdirectory, its fsmonitor armed.

    Measured (git 2.34): a planted BARE repository is not exploitable this
    way -- git refuses to work from inside a git dir -- but a nested non-bare
    one with an index fires its fsmonitor from its own directory or deeper.
    ``safe.bareRepository=explicit`` stays in :func:`git_command` for the bare
    shape on the gits where it matters.
    """
    planted = repo / "sub"
    planted.mkdir()
    _git(planted, "init", "-q")
    (planted / "f.txt").write_text("x\n", encoding="utf-8")
    (planted / "deeper").mkdir()
    (planted / "deeper" / "g.txt").write_text("y\n", encoding="utf-8")
    _git(planted, "add", ".")
    _git(planted, "config", "core.fsmonitor", f"touch {marker}")
    return planted


@pytest.mark.parametrize("where", [".", "deeper"])
def test_the_threat_is_real_plain_git_from_inside_runs_the_planted_fsmonitor(
    repo: Path, tmp_path: Path, where: str
) -> None:
    """The premise of the next test: without the hardening, the plant fires."""
    marker = tmp_path / "pwned"
    planted = _plant_nested_repository(repo, marker)
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=planted / where,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        check=False,
    )
    assert marker.exists()


def test_a_repository_planted_in_a_subdirectory_is_never_run(repo: Path, tmp_path: Path) -> None:
    """The planted repository's fsmonitor must never run through git_command."""
    marker = tmp_path / "pwned"
    _plant_nested_repository(repo, marker)
    completed = subprocess.run(
        [*git_command(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        env=git_tripwire.git_environment(os.environ, repo),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_git_environment_bounds_discovery_and_drops_inherited_git_variables(repo: Path) -> None:
    env = git_tripwire.git_environment(
        {"PATH": "/usr/bin", "GIT_DIR": "/evil", "GIT_WORK_TREE": "/evil", "HOME": "/h"}, repo
    )
    assert env["GIT_CEILING_DIRECTORIES"] == str(repo.parent)
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/h"
