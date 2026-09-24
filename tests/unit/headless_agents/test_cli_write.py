"""``ha run --write`` (spec 3.4) on real throwaway repositories.

The provider is a fake that edits its workspace like an agent would; the
worktree, the carrier commit, the repository's hooks, the tripwire and
``ha clean`` are the real code and a real ``git``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.spec import RunSpec
from headless_agents.workspace import workspace_summary

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


Edit = Callable[[Path], None]


@dataclass
class _Agent:
    """A provider that applies ``edit`` to its workspace and answers."""

    name: str
    edit: Edit | None = None
    code: int = 0
    tampered: tuple[str, ...] = ()
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        workspace = spec.profile.workspace
        assert workspace is not None and workspace.write
        if self.edit is not None:
            self.edit(workspace.path)
        spec = spec.with_run_dir_defaults()
        return record(
            spec,
            RunResult(
                exit_code=1 if self.tampered else self.code,
                provider=self.name,
                model=spec.model,
                model_reported="served-model",
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=0.1,
                tool_call_completed=False,
                text="I changed things" if self.code == 0 and not self.tampered else None,
                run_id=run_id_of(spec),
                workspace=workspace_summary(workspace, self.tampered),
            ),
        )


@dataclass
class _World:
    home: Path
    repo: Path
    environ: dict[str, str]
    agent: _Agent

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ=self.environ,
            stdin=io.StringIO(),
            stdout=out,
            stderr=err,
            cwd=self.repo,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()

    def only_run_dir(self) -> Path:
        (run_dir,) = list((self.home / ".cache" / "ha" / "runs").iterdir())
        return run_dir


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Op")
    _git(repo, "config", "user.email", "op@example.test")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    agent = _Agent("codex")
    monkeypatch.setattr(cli, "get_provider", lambda name: agent)
    monkeypatch.setenv("HOME", str(home))
    environ = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return _World(home=home, repo=repo, environ=environ, agent=agent)


def _edit_app(root: Path) -> None:
    (root / "app.py").write_text("print('v2')\n")
    (root / "new.py").write_text("x = 1\n")


def test_write_commits_the_change_on_a_carrier_branch(world: _World) -> None:
    world.agent.edit = _edit_app
    code, out, _ = world.run("run", "-p", "codex", "--write", "improve app")
    assert code == 0
    run_dir = world.only_run_dir()
    branch = f"ha/{run_dir.name}"
    log = _git(world.repo, "log", "--format=%s", "-1", branch)
    assert log.strip() == f"chore(ha): {run_dir.name} via codex/served-model"
    assert _git(world.repo, "show", f"{branch}:app.py") == "print('v2')\n"
    # never merged: main is untouched
    assert (world.repo / "app.py").read_text() == "print('v1')\n"
    assert _git(world.repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert branch in out and "app.py" in out and "I changed things" in out
    patch = run_dir / "change.patch"
    assert patch.is_file() and "print('v2')" in patch.read_text()
    assert json.loads((run_dir / "result.json").read_text())["branch"] == branch


def test_the_agent_works_in_a_worktree_of_the_repository(world: _World) -> None:
    world.agent.edit = _edit_app
    world.run("run", "-p", "codex", "--write", "go")
    workspace = world.agent.specs[0].profile.workspace
    assert workspace is not None
    assert workspace.path == world.only_run_dir() / "wt"
    assert workspace.shell is False


def test_shell_is_passed_through(world: _World) -> None:
    world.agent.edit = _edit_app
    world.run("run", "-p", "codex", "--write", "--shell", "go")
    workspace = world.agent.specs[0].profile.workspace
    assert workspace is not None and workspace.shell is True


def test_write_defaults_to_the_full_context(world: _World) -> None:
    (world.repo / "CLAUDE.md").write_text("ignored rules\n")
    world.agent.edit = _edit_app
    world.run("run", "-p", "codex", "--write", "go")
    context = world.agent.specs[0].context
    assert context is not None and context.level == "full"


def test_no_change_exits_5_and_commits_nothing(world: _World) -> None:
    code, out, err = world.run("run", "-p", "codex", "--write", "go")
    assert code == 5
    run_dir = world.only_run_dir()
    assert _git(world.repo, "rev-parse", f"ha/{run_dir.name}") == _git(
        world.repo, "rev-parse", "main"
    )
    assert "no change" in err


def test_a_refusing_hook_keeps_the_diff_uncommitted_and_exits_1(world: _World) -> None:
    hook = world.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    world.agent.edit = _edit_app
    code, _, err = world.run("run", "-p", "codex", "--write", "go")
    assert code == 1
    run_dir = world.only_run_dir()
    assert "lint says no" in (run_dir / "commit.log").read_text()
    assert (run_dir / "wt" / "app.py").read_text() == "print('v2')\n"
    assert _git(world.repo, "rev-parse", f"ha/{run_dir.name}") == _git(
        world.repo, "rev-parse", "main"
    )
    assert "hook" in err


def test_a_passing_hook_runs(world: _World) -> None:
    marker = world.home / "hook-ran"
    hook = world.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    world.agent.edit = _edit_app
    code, *_ = world.run("run", "-p", "codex", "--write", "go")
    assert code == 0 and marker.exists()


def test_a_tampered_run_never_runs_git(world: _World) -> None:
    """The rail's tripwire fired: no commit, no diff, the worktree kept for inspection."""
    world.agent.edit = _edit_app
    world.agent.tampered = ("/somewhere/.git/hooks/pre-commit",)
    code, _, err = world.run("run", "-p", "codex", "--write", "go")
    assert code == 1
    assert "tripwire" in err
    run_dir = world.only_run_dir()
    assert not (run_dir / "change.patch").exists()
    assert _git(world.repo, "rev-parse", f"ha/{run_dir.name}") == _git(
        world.repo, "rev-parse", "main"
    )


def test_the_cli_arms_its_own_tripwire_whatever_the_provider_reports(world: _World) -> None:
    """Defence in depth: a provider that plants a hook and reports nothing is still caught."""

    def plant(root: Path) -> None:
        _edit_app(root)
        common = Path(_git(root, "rev-parse", "--git-common-dir").strip())
        common = common if common.is_absolute() else root / common
        (common / "hooks" / "post-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n")

    world.agent.edit = plant
    code, _, err = world.run("run", "-p", "codex", "--write", "go")
    assert code == 1
    assert "post-commit" in err
    run_dir = world.only_run_dir()
    assert json.loads((run_dir / "result.json").read_text())["workspace"]["git_tampered"]


def test_a_failed_agent_run_commits_nothing(world: _World) -> None:
    world.agent.edit = _edit_app
    world.agent.code = 1
    code, *_ = world.run("run", "-p", "codex", "--write", "go")
    assert code == 1
    run_dir = world.only_run_dir()
    assert _git(world.repo, "rev-parse", f"ha/{run_dir.name}") == _git(
        world.repo, "rev-parse", "main"
    )


def test_write_outside_a_git_repository_is_a_usage_error(world: _World, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    code, _, err = world.run("run", "-p", "codex", "--write", "--repo", str(plain), "go")
    assert code == 2 and "git" in err


def test_a_named_base_is_used(world: _World) -> None:
    first = _git(world.repo, "rev-parse", "HEAD").strip()
    (world.repo / "later.txt").write_text("later\n")
    _git(world.repo, "add", "later.txt")
    _git(world.repo, "commit", "-q", "-m", "later")
    world.agent.edit = _edit_app
    world.run("run", "-p", "codex", "--write", "--base", first, "go")
    run_dir = world.only_run_dir()
    assert _git(world.repo, "rev-parse", f"ha/{run_dir.name}~1").strip() == first


def test_json_carries_the_branch(world: _World) -> None:
    world.agent.edit = _edit_app
    code, out, _ = world.run("run", "-p", "codex", "--write", "--json", "go")
    assert code == 0
    payload = json.loads(out)
    assert payload["branch"] == f"ha/{world.only_run_dir().name}"


# ── ha clean ───────────────────────────────────────────────────────────────


def test_clean_removes_the_worktree_and_keeps_the_branch(world: _World) -> None:
    world.agent.edit = _edit_app
    world.run("run", "-p", "codex", "--write", "go")
    run_dir = world.only_run_dir()
    code, _, err = world.run("clean", run_dir.name)
    assert code == 0
    assert not run_dir.exists()
    assert str(run_dir / "wt") not in _git(world.repo, "worktree", "list")
    assert _git(world.repo, "rev-parse", "--verify", f"ha/{run_dir.name}")


def test_clean_of_a_tampered_run_never_runs_git(world: _World) -> None:
    world.agent.edit = _edit_app
    world.agent.tampered = ("/x/.git/config",)
    world.run("run", "-p", "codex", "--write", "go")
    run_dir = world.only_run_dir()
    code, _, err = world.run("clean", run_dir.name)
    assert code == 0
    assert not run_dir.exists()
    assert "git worktree prune" in err
