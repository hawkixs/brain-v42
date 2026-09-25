"""``ha`` -- hand a task to any provider or role from a terminal or a session (spec 0.5.0 §3.9).

.. code-block:: text

    ha run TARGET [PROMPT | -] [options]     TARGET: a role or a provider
    ha roles [--json]
    ha providers [--json]
    ha runs [--limit N] [--json]
    ha show RUN_ID [--json]
    ha clean RUN_ID
    ha --version

A thin adapter: it parses arguments and prints. Every rule lives in
:mod:`headless_agents.engine`, which every entry point shares (§3.4) -- a gate
the CLI alone enforced would leak through a library caller.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from typing import IO, Final

from . import lineage as lineages
from . import quarantine, show
from .capability import INVALID_USAGE_EXIT_CODE
from .config_paths import state_dir
from .engine import (
    Overrides,
    Request,
    UsageError,
    clean,
    describe_roles,
    executable_for,
    execute,
    plan,
    runs_root,
)
from .registry import PROVIDER_NAMES, Probe, UnknownProvider, max_prompt_bytes, probe
from .report import RUN_JSON
from .run_record import RESULT_FILE_NAME
from .runs import Registry, RegistryError
from .show import format_cost, format_duration, load_json, read_run_dir, read_task
from .state import Unknown
from .write_flow import PATCH_FILE

__all__ = ["PROVIDER_NAMES", "Probe", "UnknownProvider", "main"]

INTERRUPTED_EXIT_CODE: Final = 130
#: ``ha runs`` shows the first line of a task up to this many characters (spec §3.9).
TASK_WIDTH: Final = 60

_RUN_EPILOG: Final = """\
exit codes of a run (a role or a provider):
  0 the answer
  1 failure
  2 invalid usage or configuration; nothing ran
  3 provider unavailable (a chain that runs out of links returns its last link's 3 or 4)
  4 timeout with no tool call started
  5 write run with no change
  124 timeout
  130 interrupted (Ctrl-C); the run reads incomplete

examples:
  ha run codex -m gpt-6-luna "Explain what this repository does."
  ha run reviewer-codex - < task.md
  ha run claude --context full --json "Summarise the open TODOs." > run.json
"""


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


def _store_true_or_none(parser: argparse.ArgumentParser, *flags: str, help: str) -> None:
    """A flag that is ``True`` when given and ``None`` when absent: an override."""
    parser.add_argument(*flags, action="store_const", const=True, default=None, help=help)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha", description="Run a task on any agent provider or declared role."
    )
    parser.add_argument(
        "--version", action="store_true", help="print the installed version of ha and exit"
    )
    commands = parser.add_subparsers(dest="command")

    providers = commands.add_parser("providers", help="list the providers and their availability")
    providers.add_argument("--json", action="store_true", help="print the list as JSON")

    run = commands.add_parser(
        "run",
        help="run one task on a role or a provider",
        epilog=_RUN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("target", help="a role declared in roles.toml, or a provider name")
    run.add_argument(
        "prompt", nargs="?", help="the task; '-' reads stdin; absent reads a piped stdin"
    )
    run.add_argument("-m", "--model", help="the model of every link that names none")
    run.add_argument("--effort", help="reasoning effort, where the rail takes one")
    run.add_argument("--timeout", type=float, help="seconds for this run")
    run.add_argument("--context", choices=("full", "global", "none"), help="context bundle level")
    _store_true_or_none(
        run, "--context-parents", help="add the parent directories' instruction files"
    )
    run.add_argument("--mcp", metavar="PROFILE", help="an MCP profile of mcp.toml")
    _store_true_or_none(
        run, "--write", help="a writable run on a new ha/<run_id> branch (spec §3.8.3)"
    )
    _store_true_or_none(run, "--shell", help="the unconfined shell of a write run")
    run.add_argument("--base", metavar="REF", help="the base of a write run")
    run.add_argument("--repo", type=Path, help="the repository (default: the one holding cwd)")
    run.add_argument("--base-url", help="the endpoint of openai-compat")
    run.add_argument("--key-env", metavar="VAR", help="the key variable of openai-compat")
    run.add_argument("--json", action="store_true", help="print run.json")
    run.add_argument("--run-dir", type=Path, help="the run's directory (must not exist)")
    # Removed in 0.5.0: kept hidden so their use gets a message, not argparse's guess.
    run.add_argument("-p", "--provider", help=argparse.SUPPRESS)
    run.add_argument("--chain", help=argparse.SUPPRESS)

    roles = commands.add_parser("roles", help="list the roles declared in roles.toml")
    roles.add_argument("--json", action="store_true", help="print the list as JSON")

    runs = commands.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=20, help="how many runs to list")
    runs.add_argument("--json", action="store_true", help="print the list as JSON")

    show_parser = commands.add_parser("show", help="show one run, rebuilt from the state")
    show_parser.add_argument("run_id", help="the run id, as ha run or ha runs printed it")
    show_parser.add_argument("--json", action="store_true", help="print the rebuilt run.json")

    clean_parser = commands.add_parser("clean", help="remove one run's directory")
    clean_parser.add_argument("run_id", help="the run id, as ha run printed it")
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


# ── ha providers ────────────────────────────────────────────────────────────


def _providers(args: argparse.Namespace, io: Io) -> int:
    rows = []
    for name in PROVIDER_NAMES:
        found = probe(name, executable=executable_for(name, io.home), environ=io.environ)
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


def _prompt(args: argparse.Namespace, io: Io) -> tuple[str | None, bool]:
    """The task text and whether stdin is a terminal (§3.9: never wait on one)."""
    is_tty = bool(getattr(io.stdin, "isatty", lambda: False)())
    if args.prompt == "-":
        return io.stdin.read(), is_tty
    if args.prompt is not None:
        return args.prompt, is_tty
    return (None if is_tty else io.stdin.read()), is_tty


def _run(args: argparse.Namespace, io: Io) -> int:
    if args.provider is not None:
        raise UsageError(
            '-p was removed in 0.5.0: the provider is the TARGET (ha run codex "..."); '
            "a chain is declared in roles.toml"
        )
    if args.chain is not None:
        raise UsageError(
            "--chain was removed in 0.5.0: declare the chain in a role of roles.toml "
            '(chain = ["codex:MODEL", "claude:MODEL"]) and run the role as the TARGET'
        )
    prompt, is_tty = _prompt(args, io)
    request = Request(
        target=args.target,
        prompt=prompt,
        stdin_is_tty=is_tty,
        overrides=Overrides(
            model=args.model,
            effort=args.effort,
            timeout=args.timeout,
            context=args.context,
            context_parents=args.context_parents,
            mcp=args.mcp,
            write=args.write,
            # nosec B604: ``shell`` is a role capability override, not a subprocess argument.
            shell=args.shell,  # nosec B604
            base_url=args.base_url,
            key_env=args.key_env,
        ),
        base=args.base,
        repo=args.repo,
        run_dir=args.run_dir,
        cwd=io.cwd,
        environ=io.environ,
        home=io.home,
    )
    outcome = execute(plan(request), say=io.say)
    branch = outcome.report.get("branch")
    if args.json:
        io.stdout.write(json.dumps(outcome.report, ensure_ascii=False, indent=2) + "\n")
    elif outcome.exit_code == 0 and outcome.final is not None and outcome.final.text:
        if isinstance(branch, str):
            io.stdout.write(f"branch: {branch}\npatch: {outcome.run_dir / PATCH_FILE}\n\n")
        text = outcome.final.text
        io.stdout.write(text if text.endswith("\n") else text + "\n")
    if outcome.exit_code != 0:
        provider = outcome.final.provider if outcome.final is not None else args.target
        io.say(
            f"{provider} exited {outcome.exit_code}; run {outcome.run_id}, "
            f"logs in {outcome.run_dir}"
        )
    return outcome.exit_code


# ── ha roles ────────────────────────────────────────────────────────────────


def _roles(args: argparse.Namespace, io: Io) -> int:
    rows = describe_roles(io.environ, io.home)
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for row in rows:
        links = row["links"]
        assert isinstance(links, list)
        chain = ", ".join(f"{link['provider']}:{link['model'] or '(no model)'}" for link in links)
        flags = [name for name in ("write", "shell") if row[name]]
        io.stdout.write(
            f"{row['name']:<20} {chain}  effort {row['effort']}  timeout {row['timeout']:g}s  "
            f"context {row['context']}"
            + (f"  mcp {row['mcp']}" if row["mcp"] else "")
            + (f"  {'+'.join(flags)}" if flags else "")
            + (f"  instructions {row['instructions_bytes']} B" if row["instructions_bytes"] else "")
            + "\n"
        )
    return 0


# ── ha runs / ha clean ──────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = load_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _first_line(text: object) -> str | None:
    return text.strip().splitlines()[0] if isinstance(text, str) and text.strip() else None


def _admitted_at(registry: Registry, run_id: str) -> int:
    """When the run was admitted: its lifecycle lock file is created then and never rewritten."""
    try:
        return registry.lifecycle_lock(run_id).stat().st_mtime_ns
    except OSError:
        return 0


def _registered_row(registry: Registry, run_id: str) -> dict[str, object]:
    """One listing row: identity and status from the registry entry, the one
    authority (§3.8.1); exit code, duration, cost and answer only from a
    ``run.json`` whose ``run_id`` is this entry's -- a report is display data,
    never trusted to name a run (codex review of #207, round 3); the task from
    the run's ``prompt.md``, while its directory is still its own
    (:func:`headless_agents.show.read_run_dir`)."""
    row: dict[str, object] = {
        "run_id": run_id,
        "target": None,
        "status": "unknown",
        "exit_code": None,
        "duration_seconds": None,
        "cost_usd": None,
        "cost_complete": None,
        "task": None,
        "text": None,
        "cleaned": False,
    }
    try:
        entry = registry.resolve(run_id)
    except (RegistryError, Unknown):
        return row
    lineage_status: str | None = None
    if entry.lineage is not None:
        # A write run's status lives in its lineage state only (§3.8.1); a
        # readable lineage silent about the run gives no status (plan P6).
        try:
            members = lineages.load(registry.state, entry.lineage).members
        except Unknown:
            lineage_status = "unknown"
        else:
            lineage_status = members.get(run_id, "unknown")
    report, own, _ = read_run_dir(entry)
    row.update(
        target=entry.target.get("name"),
        status=lineage_status
        if lineage_status == "unknown"
        else registry.effective_status(entry, lineage_status),
        cleaned=entry.cleaned_at is not None,
        task=read_task(entry.run_dir) if own else None,
    )
    if report is not None:
        row.update(
            exit_code=report.get("exit_code"),
            duration_seconds=report.get("duration_seconds"),
            cost_usd=report.get("cost_usd"),
            cost_complete=report.get("cost_complete"),
            text=_first_line(report.get("text")),
        )
    return row


def _quarantine_line(entry: Mapping[str, object]) -> str:
    """One quarantine in force (spec §3.8.5); an unreadable one still refuses."""
    if entry.get("readable"):
        return (
            f"QUARANTINE {entry.get('scope')}: {entry.get('reason')} in run {entry.get('run_id')} "
            f"({entry.get('file')}); lift it by hand after inspection"
        )
    return f"QUARANTINE unreadable: {entry.get('reason')}; lift it by hand after inspection"


def _runs_line(row: Mapping[str, object]) -> str:
    """run id, target, status, exit code, duration, cost, first line of the task (§3.9)."""
    task = row.get("task")
    shown = task if isinstance(task, str) else "-"
    if len(shown) > TASK_WIDTH:
        shown = shown[: TASK_WIDTH - 1] + "…"
    exit_code = "-" if row.get("exit_code") is None else str(row.get("exit_code"))
    return (
        f"{row.get('run_id')}  {row.get('target') or '-'!s:<16} {row.get('status')!s:<10} "
        f"exit {exit_code:<4} {format_duration(row.get('duration_seconds')):>6}  "
        f"{format_cost(row.get('cost_usd')):>6}  {shown}"
    ).rstrip()


def _runs(args: argparse.Namespace, io: Io) -> int:
    """The runs, newest first, the active quarantines above them (spec §3.9, §3.8.5).

    Registered runs come from the registry -- a custom ``--run-dir`` and a
    cleaned run included; a 0.4.0 directory of the runs cache that no entry
    names is listed as ``legacy``, from its ``result.json``. ``--json`` stays a
    list of rows and names the quarantines on stderr (plan P7).
    """
    root = runs_root(io.home)
    registry = Registry(state_dir(io.environ, home=io.home), runs_root=root)
    registered = registry.run_ids()
    entries: list[tuple[int, dict[str, object]]] = [
        (_admitted_at(registry, run_id), _registered_row(registry, run_id)) for run_id in registered
    ]
    if root.is_dir():
        for run_dir in root.iterdir():
            if run_dir.name in registered or (run_dir / RUN_JSON).exists():
                continue
            legacy = _read_json(run_dir / RESULT_FILE_NAME)
            if legacy is not None:
                entries.append(
                    (
                        (run_dir / RESULT_FILE_NAME).stat().st_mtime_ns,
                        {
                            "run_id": run_dir.name,
                            "target": legacy.get("provider"),
                            "status": "legacy",
                            "exit_code": legacy.get("exit_code"),
                            "duration_seconds": legacy.get("duration_seconds"),
                            "cost_usd": legacy.get("cost_usd"),
                            "cost_complete": isinstance(legacy.get("cost_usd"), int | float),
                            # A 0.4.0 run kept no task.
                            "task": None,
                            "text": _first_line(legacy.get("text")),
                            "cleaned": False,
                        },
                    )
                )
    entries.sort(key=lambda item: item[0], reverse=True)
    rows = [row for _, row in entries[: max(0, args.limit)]]
    quarantines = quarantine.active(registry.state)
    if args.json:
        for entry in quarantines:
            io.say(_quarantine_line(entry).replace("QUARANTINE", "quarantine", 1))
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for entry in quarantines:
        io.stdout.write(_quarantine_line(entry) + "\n")
    for row in rows:
        io.stdout.write(_runs_line(row) + "\n")
    return 0


def _clean(args: argparse.Namespace, io: Io) -> int:
    return clean(args.run_id, environ=io.environ, home=io.home, say=io.say)


# ── ha show ─────────────────────────────────────────────────────────────────


def _show(args: argparse.Namespace, io: Io) -> int:
    """``ha show RUN_ID``: rebuilt from the state (plan P6); 1 when part of it is unreadable."""
    try:
        shown = show.rebuild(
            args.run_id, state=state_dir(io.environ, home=io.home), runs_root=runs_root(io.home)
        )
    except show.NotShown as exc:
        raise UsageError(str(exc)) from None
    for note in shown.notes:
        io.say(note)
    if args.json:
        io.stdout.write(json.dumps(shown.report, ensure_ascii=False, indent=2) + "\n")
    else:
        io.stdout.write(show.render(shown))
    return 1 if shown.unknown else 0


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
        if args.version:
            io.stdout.write(f"ha {package_version('headless-agents')}\n")
            return 0
        if args.command == "providers":
            return _providers(args, io)
        if args.command == "run":
            return _run(args, io)
        if args.command == "roles":
            return _roles(args, io)
        if args.command == "runs":
            return _runs(args, io)
        if args.command == "show":
            return _show(args, io)
        if args.command == "clean":
            return _clean(args, io)
        raise UsageError("a command is required: run, roles, providers, runs, show or clean")
    except UsageError as exc:
        io.say(str(exc))
        return INVALID_USAGE_EXIT_CODE
    except ValueError as exc:
        # A provider refusing its spec (an HTTP provider with a workspace, say).
        io.say(str(exc))
        return INVALID_USAGE_EXIT_CODE
    except KeyboardInterrupt:
        # The engine released its locks and the rail killed its provider on the
        # way out; the run reads incomplete (spec §3.10).
        io.say("interrupted: the run reads incomplete")
        return INTERRUPTED_EXIT_CODE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
