"""``ha run --write``: a worktree, a writable run, a carrier commit (spec 3.4).

1. ``git worktree add <run_dir>/wt -b ha/<run_id> <base>`` (base: ``HEAD``).
2. The provider runs with ``Workspace(path=wt, write=True, shell=--shell)``,
   the context bundle (``full`` by default) delivered through its preamble
   (codex has its sandboxed shell whatever ``--shell`` says: spec decision 13).
3. The ``.git`` tripwire (ticket 0b622f47) is read twice: the rail's own, and
   one this module arms around the whole run -- a provider that planted
   something and reported nothing is still caught. If either fired, **no git
   command runs**: the worktree is kept for inspection, the run exits ``1``.
4. No change → exit ``5``. Otherwise the change is committed on ``ha/<run_id>``
   as ``chore(ha): <run_id> via <provider>/<model>`` -- a review carrier, not a
   final commit. The repository's hooks RUN: never ``--no-verify``, never a
   ``core.hooksPath`` override. A refusal leaves the diff uncommitted in the
   worktree, keeps the hook output in ``commit.log``, and exits ``1``.
5. Branch, diffstat, the patch path and the agent's text are printed.
6. **Never merge.** The caller reads the diff, then integrates or ``ha clean``.

Every git command goes through :func:`headless_agents.git_tripwire.git_command`
with :func:`~headless_agents.git_tripwire.git_environment`: pinned repository
and work tree, fsmonitor off, no implicit bare repository, bounded discovery,
never from a subdirectory of the agent-written tree.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .git_tripwire import GitTampered, Tripwire, git_command, git_environment
from .profile import Workspace
from .result import RunResult
from .run_record import RESULT_FILE_NAME
from .workspace import workspace_summary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .cli import Io, RunPlan

WORKTREE_META: Final = "worktree.json"
COMMIT_LOG: Final = "commit.log"
PATCH_FILE: Final = "change.patch"
GIT_TIMEOUT_SECONDS: Final = 120


def _git(
    root: Path,
    args: Sequence[str],
    environ: Mapping[str, str],
    *,
    tampered: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*git_command(root, tampered=tampered), *args],
        capture_output=True,
        text=True,
        env=git_environment(environ, root),
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _prepare_worktree(plan: RunPlan, args: argparse.Namespace, io: Io) -> tuple[Path, str, str]:
    """Create the worktree; ``(worktree, branch, base_commit)``, or a usage error."""
    from .cli import UsageError  # noqa: PLC0415

    repository = plan.repository
    try:
        resolved = _git(
            repository, ["rev-parse", "--verify", f"{args.base}^{{commit}}"], io.environ
        )
    except GitTampered:
        raise UsageError(f"--write needs a git repository; {repository} is not one") from None
    if resolved.returncode != 0:
        raise UsageError(f"--write: cannot resolve --base {args.base!r} in {repository}")
    base_commit = resolved.stdout.strip()
    worktree = plan.run_dir / "wt"
    branch = f"ha/{plan.run_dir.name}"
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    added = _git(
        repository, ["worktree", "add", "-q", "-b", branch, str(worktree), base_commit], io.environ
    )
    if added.returncode != 0:
        raise UsageError(f"--write: git worktree add failed: {added.stderr.strip()}")
    (plan.run_dir / WORKTREE_META).write_text(
        json.dumps(
            {
                "repository": str(repository),
                "worktree": str(worktree),
                "branch": branch,
                "base": base_commit,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return worktree, branch, base_commit


def _rewrite_result(run_dir: Path, result: RunResult, branch: str | None) -> None:
    payload = result.to_dict()
    payload["branch"] = branch
    partial = run_dir / f".{RESULT_FILE_NAME}.partial"
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(run_dir / RESULT_FILE_NAME)


def run_write(plan: RunPlan, args: argparse.Namespace, io: Io) -> int:
    from .cli import NO_CHANGE_EXIT_CODE, report, run_links  # noqa: PLC0415

    worktree, branch, base_commit = _prepare_worktree(plan, args, io)
    # nosec B604: ``shell`` is a Workspace capability flag, not a subprocess argument.
    workspace = Workspace(path=worktree, write=True, shell=args.shell)  # nosec B604
    own_tripwire = Tripwire.arm(workspace, home=io.home, environ=io.environ)
    result = run_links(plan, args, io, workspace=workspace)

    raw = (result.workspace or {}).get("git_tampered")
    reported: tuple[str, ...] = tuple(str(path) for path in raw) if isinstance(raw, list) else ()
    own = own_tripwire.tampered() if own_tripwire is not None else ()
    tampered = tuple(sorted(set(reported) | set(own)))
    if tampered:
        result = replace(
            result, exit_code=1, text=None, workspace=workspace_summary(workspace, tampered)
        )
        _rewrite_result(plan.run_dir, result, None)
        report(result, plan.run_dir, args, io)
        for path in tampered:
            io.say(f"git tripwire: {path} changed during the run")
        io.say(
            f"no git command was run; the worktree is kept for inspection at {worktree}. "
            "Do not run git inside it."
        )
        return 1

    if result.exit_code != 0:
        report(result, plan.run_dir, args, io)
        io.say(f"nothing committed; the worktree is kept at {worktree}")
        return result.exit_code

    status = _git(worktree, ["status", "--porcelain"], io.environ)
    if status.returncode != 0:
        io.say(f"git status failed in {worktree}: {status.stderr.strip()}")
        return 1
    if not status.stdout.strip():
        report(result, plan.run_dir, args, io)
        io.say(f"no change in {worktree}; nothing committed")
        return NO_CHANGE_EXIT_CODE

    model = result.model_reported or result.model or "unknown"
    message = f"chore(ha): {plan.run_dir.name} via {result.provider}/{model}"
    added = _git(worktree, ["add", "-A"], io.environ)
    committed = (
        _git(worktree, ["commit", "-q", "-m", message], io.environ)
        if added.returncode == 0
        else added
    )
    (plan.run_dir / COMMIT_LOG).write_text(
        committed.stdout + committed.stderr, encoding="utf-8", errors="replace"
    )
    if committed.returncode != 0:
        result = replace(result, exit_code=1)
        _rewrite_result(plan.run_dir, result, None)
        io.say(
            f"the commit was refused (a hook, or git itself): see {plan.run_dir / COMMIT_LOG}; "
            f"the change stays uncommitted in {worktree}"
        )
        return 1

    patch = _git(worktree, ["diff", "--binary", base_commit, "HEAD"], io.environ)
    (plan.run_dir / PATCH_FILE).write_text(patch.stdout, encoding="utf-8", errors="replace")
    stat = _git(worktree, ["diff", "--stat", base_commit, "HEAD"], io.environ).stdout
    _rewrite_result(plan.run_dir, result, branch)
    if args.json:
        payload = result.to_dict()
        payload["branch"] = branch
        io.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        io.stdout.write(f"branch: {branch}\n{stat}patch: {plan.run_dir / PATCH_FILE}\n\n")
        if result.text:
            io.stdout.write(result.text if result.text.endswith("\n") else result.text + "\n")
    return 0


def remove_worktree(run_dir: Path, io: Io) -> str | None:
    """Remove the run's worktree, if it has one; a message when it cannot.

    A run whose tripwire fired never gets a git command, not even this one:
    the directory is removed by ``ha clean`` without git, and the operator is
    told to prune the worktree record after inspecting the repository.
    """
    meta_path = run_dir / WORKTREE_META
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    repository = Path(meta["repository"])
    worktree = Path(meta["worktree"])
    try:
        result = json.loads((run_dir / RESULT_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    tampered = ((result.get("workspace") or {}).get("git_tampered")) or []
    if tampered:
        io.say(
            f"the .git tripwire fired for this run: no git command is run. Once you have "
            f"inspected {repository}, run `git worktree prune` there yourself; "
            f"branch {meta['branch']} is kept."
        )
        return None
    try:
        removed = _git(repository, ["worktree", "remove", "--force", str(worktree)], io.environ)
    except GitTampered as exc:
        return str(exc)
    if removed.returncode != 0:
        return f"git worktree remove failed: {removed.stderr.strip()}"
    io.say(f"worktree removed; branch {meta['branch']} is kept")
    return None
