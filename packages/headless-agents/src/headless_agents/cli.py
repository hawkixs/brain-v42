"""``ha`` -- hand a task to any provider from a terminal or a session (spec 3.4).

.. code-block:: text

    ha providers [--json]
    ha run -p PROVIDER [-m MODEL] [--effort E] [--timeout SECONDS]
           [--chain P1,P2,...]
           [--context full|global|none] [--context-parents]
           [--mcp PROFILE]
           [--base-url URL --key-env VAR]
           [--write [--shell] [--repo PATH] [--base REF]]
           [--json] [--run-dir DIR]
           [PROMPT | -]
    ha runs [--limit N] [--json]
    ha clean RUN_ID

Exit codes (contract): ``0`` answer; ``1`` failure; ``2`` invalid usage;
``3`` provider unavailable, chain exhausted; ``4`` timeout with no tool call
started (replayable); ``5`` ``--write`` finished with no change; ``124``
timeout.

Read-only, a CLI rail runs with ``Workspace(<repository root>, write=False)``:
it can list, read and search the current repository, with no write tool and
no shell. An HTTP provider runs without a workspace. Every run writes
``~/.cache/ha/runs/<run_id>/`` (logs and ``result.json``); a chain gives each
link its own directory under ``links/`` and copies the final link's
``result.json`` to the run's root. ``--write`` is in :mod:`.cli_write`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from .capability import INVALID_USAGE_EXIT_CODE, scoped_environment
from .chain import run_chain
from .context import ContextBundle, ContextLevel, resolve_context
from .mcp_profiles import McpProfileError, mcp_server
from .profile import CapabilityProfile, Credentials, McpServer, Workspace, mcp_no_proxy_hosts
from .providers.openai_compat import GENERIC_NAME
from .registry import (
    HTTP_PROVIDER_NAMES,
    PROVIDER_NAMES,
    Probe,
    UnknownProvider,
    get_provider,
    max_prompt_bytes,
    probe,
)
from .result import RunResult
from .run_record import RESULT_FILE_NAME
from .spec import RunSpec

__all__ = ["PROVIDER_NAMES", "Probe", "UnknownProvider", "main"]

NO_CHANGE_EXIT_CODE: Final = 5
DEFAULT_TIMEOUT_SECONDS: Final = 300.0

#: The files each HOME-isolated rail needs from the operator's HOME (relative
#: to it), as the Dream and the live suite expose them. codex copies its own
#: auth into an ephemeral CODEX_HOME; claude keeps the caller's HOME.
DEFAULT_CREDENTIALS: Final[Mapping[str, tuple[str, ...]]] = {
    "agy": (
        ".gemini/oauth_creds.json",
        ".gemini/google_accounts.json",
        ".gemini/gemini-credentials.json",
        ".gemini/antigravity-cli/antigravity-oauth-token",
    ),
    "opencode": (".local/share/opencode/auth.json",),
}

#: opencode is not on PATH by default; its installer puts it here.
DEFAULT_EXECUTABLES: Final[Mapping[str, str]] = {"opencode": ".opencode/bin/opencode"}

# A Claude Code session that launches ``ha`` must not leak into a nested
# ``claude -p``: the child would no longer be the run the rail ships.
_PARENT_SESSION_PREFIXES: Final = ("CLAUDE_CODE_",)
_PARENT_SESSION_NAMES: Final = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_EFFORT"}
)


class UsageError(ValueError):
    """Invalid usage: reported on stderr, exit 2, nothing run."""


@dataclass(frozen=True)
class Io:
    environ: Mapping[str, str]
    stdin: IO[str]
    stdout: IO[str]
    stderr: IO[str]
    cwd: Path
    home: Path

    def say(self, message: str) -> None:
        self.stderr.write(f"ha: {message}\n")


# ── parsing ─────────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha", description="Run a task on any agent provider.")
    commands = parser.add_subparsers(dest="command")

    providers = commands.add_parser("providers", help="list the providers and their availability")
    providers.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="run one task")
    run.add_argument("-p", "--provider")
    run.add_argument("-m", "--model", default="")
    run.add_argument("--effort", default="medium")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--chain", help="comma-separated providers, tried in order on 3 and 4")
    run.add_argument("--context", choices=("full", "global", "none"))
    run.add_argument("--context-parents", action="store_true")
    run.add_argument("--mcp", metavar="PROFILE")
    run.add_argument("--base-url")
    run.add_argument("--key-env", metavar="VAR")
    run.add_argument("--write", action="store_true")
    run.add_argument("--shell", action="store_true")
    run.add_argument("--repo", type=Path)
    run.add_argument("--base", default="HEAD")
    run.add_argument("--json", action="store_true")
    run.add_argument("--run-dir", type=Path)
    run.add_argument("prompt", nargs="?")

    runs = commands.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--json", action="store_true")

    clean = commands.add_parser("clean", help="remove one run (and its worktree)")
    clean.add_argument("run_id")
    return parser


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = _parser()

    def fail(message: str) -> None:  # pragma: no cover - exercised through main
        raise UsageError(message)

    parser.error = fail  # type: ignore[method-assign,assignment]
    for action in parser._subparsers._group_actions if parser._subparsers else ():  # noqa: SLF001
        for sub in getattr(action, "choices", {}).values():
            sub.error = fail
    return parser.parse_args(list(argv))


# ── shared helpers ──────────────────────────────────────────────────────────


def runs_root(home: Path) -> Path:
    return home / ".cache" / "ha" / "runs"


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def operator_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if key not in _PARENT_SESSION_NAMES and not key.startswith(_PARENT_SESSION_PREFIXES)
    }


def repository_root(cwd: Path) -> Path:
    """The git work tree holding ``cwd`` (the operator's own checkout), else ``cwd``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    top = completed.stdout.strip()
    return Path(top) if completed.returncode == 0 and top else cwd


def _executable(provider: str, home: Path) -> str | None:
    relative = DEFAULT_EXECUTABLES.get(provider)
    if relative is None:
        return None
    candidate = home / relative
    return str(candidate) if candidate.is_file() and shutil.which(provider) is None else None


# ── ha providers ────────────────────────────────────────────────────────────


def _providers(args: argparse.Namespace, io: Io) -> int:
    rows = []
    for name in PROVIDER_NAMES:
        found = probe(name, executable=_executable(name, io.home), environ=io.environ)
        rows.append(
            {
                "name": name,
                "available": found.available,
                "detail": found.detail,
                "version": found.version,
                "max_prompt_bytes": max_prompt_bytes(name),
            }
        )
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for row in rows:
        mark = "ok " if row["available"] else "-- "
        io.stdout.write(f"{mark}{row['name']:<14} {row['detail']}\n")
    return 0


# ── ha run ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunPlan:
    """Everything ``ha run`` resolved before any provider starts."""

    providers: tuple[str, ...]
    prompt: str
    context: ContextBundle | None
    mcp: McpServer | None
    environment: dict[str, str]
    run_dir: Path
    repository: Path


def _prompt(args: argparse.Namespace, io: Io) -> str:
    text = io.stdin.read() if args.prompt in (None, "-") else args.prompt
    if not text.strip():
        raise UsageError("no prompt: pass it as an argument, or on stdin with '-'")
    return text


def _providers_of(args: argparse.Namespace) -> tuple[str, ...]:
    if args.chain:
        names = tuple(name.strip() for name in args.chain.split(",") if name.strip())
    elif args.provider:
        names = (args.provider,)
    else:
        raise UsageError("name a provider with -p, or a chain with --chain")
    for name in names:
        if name not in PROVIDER_NAMES:
            raise UsageError(f"unknown provider {name!r}; valid names: {', '.join(PROVIDER_NAMES)}")
    return names


def _check_flags(args: argparse.Namespace, providers: tuple[str, ...]) -> None:
    has_generic = GENERIC_NAME in providers
    endpoint_flags = args.base_url is not None or args.key_env is not None
    if has_generic and (args.base_url is None or args.key_env is None):
        raise UsageError("openai-compat needs both --base-url and --key-env")
    if endpoint_flags and not has_generic:
        raise UsageError("--base-url and --key-env apply to openai-compat only")
    if args.shell and not args.write:
        raise UsageError("--shell requires --write: a shell can write what read tools cannot")
    http = [name for name in providers if name in HTTP_PROVIDER_NAMES]
    if args.write and http:
        raise UsageError(f"--write needs a CLI rail; {', '.join(http)} has no tool to edit with")
    if args.mcp and http:
        raise UsageError(f"--mcp needs a CLI rail; {', '.join(http)} has no tools")


def _plan(args: argparse.Namespace, io: Io) -> RunPlan:
    providers = _providers_of(args)
    _check_flags(args, providers)
    prompt = _prompt(args, io)
    repository = args.repo.resolve() if args.repo else repository_root(io.cwd)
    level: ContextLevel = args.context or ("full" if args.write else "global")
    context = resolve_context(
        level=level,
        repository_root=repository,
        user_files=(io.home / ".claude" / "CLAUDE.md",),
        include_parents=args.context_parents,
    )
    mcp: McpServer | None = None
    if args.mcp:
        try:
            mcp = mcp_server(args.mcp, environ=io.environ)
        except McpProfileError as exc:
            raise UsageError(str(exc)) from None
    environment = operator_environment(io.environ)
    if mcp is not None and mcp_no_proxy_hosts(mcp):
        environment = {
            **environment,
            **{
                key: value
                for key, value in scoped_environment(
                    environment, no_proxy_hosts=mcp_no_proxy_hosts(mcp)
                ).items()
                if key in ("NO_PROXY", "no_proxy")
            },
        }
    # Anchored at the CLI's cwd and made absolute: ``--run-dir .`` has an empty
    # raw name, and the run id, the ``ha-<id>`` prefix and the ``ha/<id>``
    # branch all read it.
    run_dir = (
        Path(os.path.abspath(io.cwd / args.run_dir))
        if args.run_dir is not None
        else runs_root(io.home) / new_run_id()
    )
    return RunPlan(
        providers=providers,
        prompt=prompt,
        context=context,
        mcp=mcp,
        environment=environment,
        run_dir=run_dir,
        repository=repository,
    )


def spec_for(
    provider: str,
    plan: RunPlan,
    args: argparse.Namespace,
    io: Io,
    *,
    run_dir: Path,
    workspace: Workspace | None,
) -> RunSpec:
    """One link's :class:`RunSpec`."""
    http = provider in HTTP_PROVIDER_NAMES
    extra: dict[str, object] = {}
    if provider == GENERIC_NAME:
        extra = {"base_url": args.base_url, "key_env": args.key_env}
    return RunSpec(
        prompt=plan.prompt,
        name=f"ha-{run_dir.name}",
        model=args.model,
        profile=CapabilityProfile(
            mcp=None if http else plan.mcp,
            workspace=None if http else workspace,
            credentials=Credentials(paths=DEFAULT_CREDENTIALS.get(provider, ())),
        ),
        reasoning_effort=args.effort,
        max_turns=50,
        timeout_seconds=args.timeout,
        run_dir=run_dir,
        executable=_executable(provider, io.home),
        environment=plan.environment,
        context=plan.context,
        extra=extra,
    )


def run_links(
    plan: RunPlan,
    args: argparse.Namespace,
    io: Io,
    *,
    workspace: Workspace | None,
) -> RunResult:
    """Run the chain (or the single provider) and return the final link's result."""
    results: dict[str, RunResult] = {}
    chained = len(plan.providers) > 1

    def run_one(provider: str) -> int:
        index = plan.providers.index(provider)
        run_dir = plan.run_dir / "links" / f"{index}-{provider}" if chained else plan.run_dir
        result = get_provider(provider).run(
            spec_for(provider, plan, args, io, run_dir=run_dir, workspace=workspace)
        )
        results[provider] = result
        return result.exit_code

    def on_fallback(provider: str, next_provider: str) -> None:
        code = results[provider].exit_code
        io.say(f"{provider} exited {code} (nothing written); falling back to {next_provider}")

    outcome = run_chain(plan.providers, run_one=run_one, on_fallback=on_fallback)
    final = results[outcome.provider]
    if chained:
        source = (
            plan.run_dir / "links" / f"{plan.providers.index(outcome.provider)}-{outcome.provider}"
        )
        if (source / RESULT_FILE_NAME).is_file():
            shutil.copyfile(source / RESULT_FILE_NAME, plan.run_dir / RESULT_FILE_NAME)
    if outcome.dead_links:
        io.say(f"no answer within the deadline from: {', '.join(outcome.dead_links)}")
    return final


def report(result: RunResult, run_dir: Path, args: argparse.Namespace, io: Io) -> None:
    if args.json:
        io.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    elif result.exit_code == 0 and result.text is not None:
        io.stdout.write(result.text if result.text.endswith("\n") else result.text + "\n")
    if result.exit_code != 0:
        io.say(f"{result.provider} exited {result.exit_code}; logs in {run_dir}")


def _run(args: argparse.Namespace, io: Io) -> int:
    plan = _plan(args, io)
    if args.write:
        from .cli_write import run_write  # noqa: PLC0415 - the write flow is its own module

        return run_write(plan, args, io)
    workspace = Workspace(path=plan.repository)
    result = run_links(plan, args, io, workspace=workspace)
    report(result, plan.run_dir, args, io)
    return result.exit_code


# ── ha runs / ha clean ──────────────────────────────────────────────────────


def _read_result(run_dir: Path) -> dict[str, object] | None:
    try:
        payload = json.loads((run_dir / RESULT_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _runs(args: argparse.Namespace, io: Io) -> int:
    root = runs_root(io.home)
    entries: list[tuple[int, dict[str, object]]] = []
    if root.is_dir():
        for run_dir in root.iterdir():
            payload = _read_result(run_dir)
            if payload is None:
                continue
            stamp = (run_dir / RESULT_FILE_NAME).stat().st_mtime_ns
            text = payload.get("text")
            first_line = (
                text.strip().splitlines()[0] if isinstance(text, str) and text.strip() else None
            )
            entries.append(
                (
                    stamp,
                    {
                        "run_id": run_dir.name,
                        "provider": payload.get("provider"),
                        "model": payload.get("model"),
                        "exit_code": payload.get("exit_code"),
                        "duration_seconds": payload.get("duration_seconds"),
                        "branch": payload.get("branch"),
                        "text": first_line,
                    },
                )
            )
    entries.sort(key=lambda entry: entry[0], reverse=True)
    rows = [row for _, row in entries[: max(0, args.limit)]]
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for row in rows:
        io.stdout.write(
            f"{row['run_id']}  {row['provider']:<14} exit {row['exit_code']}  {row['text'] or ''}\n"
        )
    return 0


def _clean(args: argparse.Namespace, io: Io) -> int:
    run_id = args.run_id
    if not run_id or run_id in (".", "..") or "/" in run_id or os.sep in run_id:
        raise UsageError(f"not a run id: {run_id!r}")
    run_dir = runs_root(io.home) / run_id
    if not run_dir.is_dir():
        io.say(f"no run {run_id!r} under {runs_root(io.home)}")
        return 1
    from .cli_write import remove_worktree  # noqa: PLC0415

    problem = remove_worktree(run_dir, io)
    if problem is not None:
        io.say(problem)
        return 1
    shutil.rmtree(run_dir)
    return 0


# ── entry point ─────────────────────────────────────────────────────────────


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    io = Io(
        environ=environ,
        stdin=stdin if stdin is not None else sys.stdin,
        stdout=stdout if stdout is not None else sys.stdout,
        stderr=stderr if stderr is not None else sys.stderr,
        cwd=cwd if cwd is not None else Path.cwd(),
        home=home if home is not None else Path(environ.get("HOME") or Path.home()),
    )
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
        if args.command == "providers":
            return _providers(args, io)
        if args.command == "run":
            return _run(args, io)
        if args.command == "runs":
            return _runs(args, io)
        if args.command == "clean":
            return _clean(args, io)
        raise UsageError("a command is required: providers, run, runs or clean")
    except UsageError as exc:
        io.say(str(exc))
        return INVALID_USAGE_EXIT_CODE
    except ValueError as exc:
        # A provider refusing its spec (an HTTP provider with a workspace, say).
        io.say(str(exc))
        return INVALID_USAGE_EXIT_CODE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
