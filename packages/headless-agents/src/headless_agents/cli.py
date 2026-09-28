"""``ha`` -- hand a task to any provider or role from a terminal or a session (spec 0.5.0 §3.9).

.. code-block:: text

    ha run TARGET [PROMPT | -] [options]     TARGET: a workflow, a role or a provider
    ha roles [--json]
    ha workflows [--json]
    ha providers [--json]
    ha providers [RAIL...] --update [--check] [--no-prove] [--wait SECONDS] [--json]
    ha models [--provider NAME] [--json] [--refresh]
    ha runs [--limit N] [--json]
    ha show RUN_ID [--json]
    ha show --dir PATH [--json]               display only
    ha clean RUN_ID
    ha prove [RAIL...] [--isolation] [--confinement] [--stale] [--keep] [--json]
    ha --version

A thin adapter: it parses arguments and prints. Every rule lives in
:mod:`headless_agents.engine`, which every entry point shares (§3.4) -- a gate
the CLI alone enforced would leak through a library caller.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from importlib.metadata import version as package_version
from pathlib import Path
from typing import IO, Final

from . import lineage as lineages
from . import locks, prove, quarantine, show, updaters
from .capability import INVALID_USAGE_EXIT_CODE
from .cli_models import MODEL_OPTIONAL, MODELS_FILE_NAME, default_models_path, load_models
from .config_paths import config_dir, config_file, state_dir
from .engine import (
    Outcome,
    Overrides,
    Request,
    UsageError,
    clean,
    declared_roles,
    describe_roles,
    describe_workflows,
    executable_for,
    execute,
    plan,
    prompt_is_optional,
    runs_root,
)
from .locks import AdmissionWait
from .model_catalog import load_catalogue
from .model_live import live_models
from .model_report import build_model_report
from .proof_state import UNPROVABLE_CONFINEMENT, proof_status, rail_state
from .proofs import CLI_RAILS
from .registry import (
    PROVIDER_NAMES,
    Probe,
    UnknownProvider,
    max_prompt_bytes,
    probe,
    probe_environment,
)
from .report import RUN_JSON
from .run_record import RESULT_FILE_NAME
from .runs import Registry, RegistryError
from .show import (
    format_cost,
    format_diffstat,
    format_duration,
    load_json,
    read_diffstat,
    read_run_dir,
    read_task,
)
from .state import Unknown
from .structured import MAX_SCHEMA_BYTES, parse_json
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

exit codes of a workflow (implement, review):
  0 committed (implement), approved (review)
  6 changes requested (review)
  5 the implementation changed nothing (implement)
  1 a step failed (its residue committed), the tripwire fired, HEAD moved, a hook
    refused, or the verdict is unreadable; the step's own code is in run.json
  2 invalid usage or configuration, a refused --run, --continue or --findings, a
    lineage in use for more than 10 s, or the vendor rule; nothing ran
  130 interrupted (Ctrl-C); the run reads incomplete

examples:
  ha run codex -m gpt-6-luna "Explain what this repository does."
  ha run build "Add a --verbose flag to the CLI."
  ha run multi-review --run 20260926T101500-ab12cd34
  ha run build --continue 20260926T101500-ab12cd34 --findings 20260926T104000-9f8e7d6c
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

    providers = commands.add_parser(
        "providers",
        help="list the providers and their proofs; --update updates the CLI rails",
    )
    providers.add_argument(
        "rails",
        nargs="*",
        metavar="RAIL",
        help=f"with --update: a CLI rail, {', '.join(CLI_RAILS)} (default: every one of them)",
    )
    providers.add_argument("--json", action="store_true", help="print the list as JSON")
    providers.add_argument(
        "--update",
        action="store_true",
        help="update the CLI rails with their vendors' updaters, then prove the rails whose "
        "version changed (spends provider tokens)",
    )
    providers.add_argument(
        "--check",
        action="store_true",
        help="with --update: run nothing, take no lock; show what an update would run",
    )
    providers.add_argument(
        "--no-prove", action="store_true", help="with --update: prove nothing afterwards"
    )
    providers.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="with --update: wait up to SECONDS for running runs to end",
    )

    models = commands.add_parser(
        "models", help="compare the operator's model catalogue with live provider lists"
    )
    models.add_argument("--provider", choices=PROVIDER_NAMES, help="report one provider only")
    models.add_argument("--json", action="store_true", help="print the report as JSON")
    models.add_argument("--refresh", action="store_true", help="also print catalogue/live drift")

    run = commands.add_parser(
        "run",
        help="run one task on a workflow, a role or a provider",
        epilog=_RUN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "target",
        help="a workflow of workflows.toml, a role of roles.toml, or a provider name",
    )
    run.add_argument(
        "prompt", nargs="?", help="the task; '-' reads stdin; absent reads a piped stdin"
    )
    run.add_argument("-m", "--model", help="the model of every link that names none")
    run.add_argument("--effort", help="reasoning effort, where the rail takes one")
    run.add_argument("--timeout", type=float, help="seconds for this run")
    run.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="wait up to SECONDS for admission locks; requires an explicit positive bound",
    )
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
    run.add_argument("--max-tokens", type=int, metavar="N", help="maximum HTTP answer tokens")
    run.add_argument("--json", action="store_true", help="print run.json")
    run.add_argument("--run-dir", type=Path, help="the run's directory (must not exist)")
    run.add_argument(
        "--output-schema",
        type=Path,
        metavar="FILE",
        help="constrain the answer to the JSON Schema in FILE (claude and codex only; "
        "the answer must be JSON)",
    )
    run.add_argument(
        "--continue",
        dest="continue_run",
        metavar="RUN_ID",
        help="an implement workflow only: join the lineage of RUN_ID, an implement run, "
        "and work on its branch in its worktree",
    )
    run.add_argument(
        "--findings",
        dest="findings_run",
        metavar="RUN_ID",
        help="an implement workflow only: address the findings of RUN_ID, a review of the "
        "commit this run starts from; the prompt becomes optional",
    )
    run.add_argument(
        "--head",
        metavar="REF",
        help="a review workflow only: the commit reviewed (default HEAD)",
    )
    run.add_argument(
        "--run",
        dest="review_run",
        metavar="RUN_ID",
        help="a review workflow only: review the current tip of RUN_ID's lineage, from its "
        "base; excludes --head and --base",
    )
    # Removed in 0.5.0: kept hidden so their use gets a message, not argparse's guess.
    run.add_argument("-p", "--provider", help=argparse.SUPPRESS)
    run.add_argument("--chain", help=argparse.SUPPRESS)

    roles = commands.add_parser("roles", help="list the roles declared in roles.toml")
    roles.add_argument("--json", action="store_true", help="print the list as JSON")

    workflows = commands.add_parser(
        "workflows", help="list the workflows declared in workflows.toml"
    )
    workflows.add_argument("--json", action="store_true", help="print the list as JSON")

    runs = commands.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=20, help="how many runs to list")
    runs.add_argument("--json", action="store_true", help="print the list as JSON")

    show_parser = commands.add_parser("show", help="show one run, rebuilt from the state")
    show_parser.add_argument(
        "run_id", nargs="?", help="the run id, as ha run or ha runs printed it"
    )
    show_parser.add_argument(
        "--dir",
        type=Path,
        metavar="PATH",
        help="render a run directory's run.json instead, for display only",
    )
    show_parser.add_argument("--json", action="store_true", help="print the rebuilt run.json")

    clean_parser = commands.add_parser("clean", help="remove one run's directory")
    clean_parser.add_argument("run_id", help="the run id, as ha run printed it")

    prove_parser = commands.add_parser(
        "prove",
        help="record CLI rails' isolation and confinement proofs from the installed ha; "
        "spends provider tokens",
    )
    prove_parser.add_argument(
        "rails",
        nargs="*",
        metavar="RAIL",
        help=f"a CLI rail: {', '.join(CLI_RAILS)} (default: every one of them)",
    )
    prove_parser.add_argument(
        "--isolation", action="store_true", help="prove isolation (default: both kinds)"
    )
    prove_parser.add_argument(
        "--confinement", action="store_true", help="prove confinement (default: both kinds)"
    )
    prove_parser.add_argument(
        "--stale",
        action="store_true",
        help="only the proofs that have not passed on the version installed now",
    )
    prove_parser.add_argument(
        "--keep", action="store_true", help="keep the proof directory under ~/.cache/ha/proofs/"
    )
    prove_parser.add_argument(
        "--json", action="store_true", help="print the verdicts and the modes as JSON"
    )
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


def _proof_detail_line(row: dict[str, object]) -> str:
    isolation = row["isolation"]
    confinement = row["confinement"]
    assert isinstance(isolation, dict) and isinstance(confinement, dict)

    def clause(kind: str, status: dict[str, object]) -> str:
        inside = str(status["reason"])
        if status["date"]:
            inside = f"{inside}, {status['date']}"
        return f"{kind} {status['status']} ({inside})"

    version = row["version"] or "(version unknown)"
    return (
        f"     {version}: {clause('isolation', isolation)}; "
        f"{clause('confinement', confinement)}; mode {row['mode']}\n"
    )


def _providers(args: argparse.Namespace, io: Io) -> int:
    if args.update:
        return _providers_update(args, io)
    stray = [
        flag
        for flag, given in (
            ("RAIL", bool(args.rails)),
            ("--check", args.check),
            ("--no-prove", args.no_prove),
            ("--wait", args.wait is not None),
        )
        if given
    ]
    if stray:
        raise UsageError(f"{', '.join(stray)}: only with --update; nothing ran")
    state = state_dir(io.environ, home=io.home)
    rows = []
    for name in PROVIDER_NAMES:
        found = probe(
            name,
            executable=executable_for(name, io.home),
            environ=probe_environment(name, io.home, io.environ),
        )
        row: dict[str, object] = {
            "name": name,
            "available": found.available,
            "detail": found.detail,
            "version": found.version,
            "max_prompt_bytes": max_prompt_bytes(name),
        }
        if name in CLI_RAILS:
            rs = rail_state(state, name, found.version)
            row["isolation"] = asdict(rs.isolation)
            row["confinement"] = asdict(rs.confinement)
            row["mode"] = rs.mode
            row["reprove"] = rs.reprove
        else:
            row["isolation"] = None
            row["confinement"] = None
            row["mode"] = None
            row["reprove"] = None
        rows.append(row)
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for row in rows:
        mark = "ok " if row["available"] else "-- "
        io.stdout.write(f"{mark}{row['name']:<14} {row['detail']}\n")
        if row["isolation"] is not None:
            io.stdout.write(_proof_detail_line(row))
            if row["reprove"] is not None:
                io.stdout.write(f"     re-prove: {row['reprove']}\n")
    io.stdout.write(
        "mode is for a write role without --shell; a --shell write on claude, "
        "opencode or agy is always serialised\n"
    )
    return 0


# ── ha prove ────────────────────────────────────────────────────────────────


def _cli_rails(names: Sequence[str], command: str) -> list[str]:
    """The rails asked for, in the order given; every CLI rail when none is named."""
    for name in names:
        if name not in CLI_RAILS:
            raise UsageError(
                f"{name}: not a CLI rail; {command} takes {', '.join(CLI_RAILS)}; nothing ran"
            )
    return list(dict.fromkeys(names)) if names else list(CLI_RAILS)


def _prove_models(rails: Sequence[str], io: Io) -> dict[str, str]:
    """Each rail's model, from ``models.toml``: the package names none (cli_models)."""
    found = config_file(MODELS_FILE_NAME, io.environ, home=io.home)
    declared = load_models(found) if found is not None else {}
    for rail in rails:
        if not declared.get(rail) and rail not in MODEL_OPTIONAL:
            path = default_models_path(io.environ, home=io.home)
            raise UsageError(
                f'{rail} needs a model to be proven with: declare it in {path} ({rail} = "MODEL"); '
                "nothing ran"
            )
    return {rail: declared.get(rail, "") for rail in rails}


def _needs_proving(state: Path, rail: str, kind: prove.Kind, found: Probe) -> bool:
    """``--stale``: an installed rail's proof that has not passed on its version now.

    A confinement that cannot be proven is never selected: running it would spend
    tokens on a probe that stays inconclusive (``proof_state.rail_state`` offers no
    re-prove for it either).
    """
    if not found.available or (kind == "confinement" and rail in UNPROVABLE_CONFINEMENT):
        return False
    return proof_status(state, rail, kind, found.version).status != "passed"


def _verdict_line(verdict: prove.Verdict) -> str:
    if verdict.outcome == "skipped":
        return f"{verdict.rail} {verdict.kind}: skipped ({verdict.reason})"
    recorded = "recorded" if verdict.recorded else "not recorded"
    return f"{verdict.rail} {verdict.kind}: {verdict.outcome}, {recorded} ({verdict.reason})"


def _prove(args: argparse.Namespace, io: Io) -> int:
    """``ha prove``: record CLI rails' proofs from the installed package (spec 0.5.2 §3.4).

    Everything that can refuse does so before the first provider run: a name that is
    not a CLI rail, claude's unprovable confinement asked for by name, a named rail not
    installed, isolation from a development install, a rail without a model. The runs
    are then announced on stderr before they spend anything -- no question asked,
    sessions run ``ha`` headless. :func:`headless_agents.prove.prove` makes the runs and
    alone records; this command selects, announces and reports.
    """
    rails = _cli_rails(args.rails, "ha prove")
    kinds = [kind for kind in prove.KINDS if getattr(args, kind)] or list(prove.KINDS)
    if args.confinement:
        for rail in args.rails:
            if rail in UNPROVABLE_CONFINEMENT:
                raise UsageError(
                    f"{rail} confinement cannot be proven: {UNPROVABLE_CONFINEMENT[rail]}; "
                    "nothing ran"
                )
    state = state_dir(io.environ, home=io.home)

    def probed(rail: str) -> Probe:
        return probe(
            rail,
            executable=executable_for(rail, io.home),
            environ=probe_environment(rail, io.home, io.environ),
        )

    found = {rail: probed(rail) for rail in rails}
    for rail in args.rails:
        if not found[rail].available:
            raise UsageError(f"{rail} is not available: {found[rail].detail}; nothing ran")

    selected = [
        (rail, kind)
        for rail in rails
        for kind in kinds
        if not args.stale or _needs_proving(state, rail, kind, found[rail])
    ]

    def skipped(rail: str, kind: prove.Kind) -> str | None:
        if kind == "confinement" and rail in UNPROVABLE_CONFINEMENT:
            return f"unprovable: {UNPROVABLE_CONFINEMENT[rail]}"
        return None if found[rail].available else found[rail].detail

    runnable = [(rail, kind) for rail, kind in selected if skipped(rail, kind) is None]
    if any(kind == "isolation" for _, kind in runnable):
        refusal = prove.checkout_refusal(io.environ)
        if refusal is not None:
            raise UsageError(f"{refusal}; nothing ran")
    models = _prove_models(list(dict.fromkeys(rail for rail, _ in runnable)), io)

    for rail, kind in runnable:
        model = f"model {models[rail]}" if models[rail] else f"the model {rail} chooses"
        io.say(
            f"prove {rail} {kind}: {prove.planned_runs(rail, kind)} provider runs on "
            f"{found[rail].version or '(version unknown)'} ({model})"
        )
    if runnable:
        total = sum(prove.planned_runs(rail, kind) for rail, kind in runnable)
        io.say(f"{total} provider runs in total; this spends provider tokens")
    if not selected and not args.json:
        io.stdout.write(
            "nothing to prove: every provable proof of the installed rails passed on their "
            "versions\n"
        )

    root = prove.proof_root(io.home)
    verdicts: list[prove.Verdict] = []
    try:
        for rail, kind in selected:
            reason = skipped(rail, kind)
            if reason is not None:
                verdict = prove.Verdict(
                    rail, kind, found[rail].version, "skipped", reason, False, 0
                )
            else:
                verdict = prove.prove(
                    rail,
                    kind,
                    model=models[rail],
                    state=state,
                    home=io.home,
                    environ=io.environ,
                    root=root,
                )
            verdicts.append(verdict)
            if not args.json:
                io.stdout.write(_verdict_line(verdict) + "\n")
                io.stdout.flush()
    finally:
        kept = args.keep and root.is_dir()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    # The mode on the version installed NOW -- the one the engine will probe -- not the
    # one probed before the runs: a CLI that updated itself mid-proof recorded nothing.
    proven = {rail for rail, _ in runnable}
    now = {rail: probed(rail) if rail in proven else found[rail] for rail in rails}
    modes = {
        rail: rail_state(state, rail, now[rail].version).mode
        for rail in rails
        if now[rail].available
    }
    if args.json:
        report = {
            "schema": 1,
            "verdicts": [verdict.to_dict() for verdict in verdicts],
            "modes": modes,
            "kept": str(root) if kept else None,
        }
        io.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        for rail, mode in modes.items():
            io.stdout.write(f"{rail} {now[rail].version or '(version unknown)'}: mode {mode}\n")
        if kept:
            io.stdout.write(f"kept: {root}\n")
    settled = all(
        verdict.outcome == "passed" and verdict.recorded
        for verdict in verdicts
        if verdict.outcome != "skipped"
    )
    return 0 if settled else 1


# ── ha providers --update ───────────────────────────────────────────────────


def _update_line(row: updaters.UpdateRow) -> str:
    """One rail: versions, updater, verdicts, mode and, when reported, the rollback."""
    if row.status == "not installed":
        return f"{row.rail}: {row.note}"
    if row.status in ("checked", "not updated"):
        parts = [row.old_version or "(version unknown)"]
    else:
        parts = [
            f"{row.old_version or '(version unknown)'} -> "
            f"{row.new_version or '(unavailable)'} ({row.status})"
        ]
    if row.status == "checked":
        parts.append(f"updater: {' '.join(row.updater)}")
    elif row.log is not None:
        exit_code = "-" if row.exit_code is None else row.exit_code
        parts.append(f"updater exit {exit_code} (log {row.log})")
    for verdict in row.verdicts:
        recorded = "recorded" if verdict.recorded else "not recorded"
        text = f"{verdict.kind} {verdict.outcome}, {recorded}"
        if not (verdict.outcome == "passed" and verdict.recorded):
            text += f" ({verdict.reason})"
        parts.append(text)
    if row.note is not None:
        parts.append(row.note)
    parts.append(f"mode {row.mode}")
    way_back = [str(item) for item in (row.rollback_path, row.rollback_command) if item]
    if way_back:
        parts.append(f"rollback: {' or '.join(way_back)}")
    return f"{row.rail}: " + "; ".join(parts)


def _providers_update(args: argparse.Namespace, io: Io) -> int:
    """``ha providers --update``: :func:`headless_agents.updaters.run_updates` does the
    work; this refuses what it can before anything runs, and reports."""
    if args.check and args.wait is not None:
        raise UsageError("--wait with --check: --check takes no lock, nothing to wait for")
    if args.wait is not None and (not math.isfinite(args.wait) or args.wait <= 0):
        raise UsageError("--wait needs a finite number of seconds greater than zero")
    rails = _cli_rails(args.rails, "ha providers --update")
    prove_after = not args.check and not args.no_prove
    # A rail updated without a model to prove it with would stay refused: known now,
    # before any updater runs.
    models = _prove_models(rails, io) if prove_after else {}
    rows = updaters.run_updates(
        rails,
        state=state_dir(io.environ, home=io.home),
        home=io.home,
        environ=io.environ,
        wait=AdmissionWait(args.wait),
        check=args.check,
        prove_after=prove_after,
        models=models,
        say=io.say,
    )
    if args.json:
        report = {"schema": 1, "rails": [row.to_dict() for row in rows]}
        io.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        for row in rows:
            io.stdout.write(_update_line(row) + "\n")

    def _unsettled(row: updaters.UpdateRow) -> bool:
        if row.status in ("failed", "timed out", "not updated"):
            return True
        changed = row.new_version is not None and row.new_version != row.old_version
        if prove_after and changed and not row.verdicts:
            # Proving was requested and the version moved, but nothing was
            # recorded for it (a dev-install refusal, say: the note explains
            # why). The command must not read a skipped proof as success.
            return True
        return any(not (v.outcome == "passed" and v.recorded) for v in row.verdicts)

    return 1 if any(_unsettled(row) for row in rows) else 0


# ── ha models ───────────────────────────────────────────────────────────────


def _models(args: argparse.Namespace, io: Io) -> int:
    path = config_file("catalog.toml", io.environ, home=io.home)
    if path is None:
        catalog_path = config_dir(io.environ, home=io.home) / "catalog.toml"
        raise UsageError(
            f"{catalog_path}: missing; the ha-delegate skill (red-skills) installs a template"
        )
    catalogue = load_catalogue(path)
    roles, _ = declared_roles(io.environ, io.home)
    defaults_path = config_file("models.toml", io.environ, home=io.home)
    defaults = load_models(defaults_path) if defaults_path is not None else {}
    names = (args.provider,) if args.provider else PROVIDER_NAMES
    live = {name: live_models(name, environ=io.environ, home=io.home) for name in names}
    rows = build_model_report(catalogue, live, roles, defaults, today=date.today())
    rows = [row for row in rows if row["provider"] in names]
    if args.json:
        io.stdout.write(
            json.dumps(
                {"schema": 1, "warnings": list(catalogue.warnings), "providers": rows},
                indent=2,
            )
            + "\n"
        )
        return 0
    for warning in catalogue.warnings:
        io.say(f"catalogue warning: {warning}")
    for row in rows:
        io.stdout.write(f"{row['provider']}: {row['live_status']} ({row['live_detail']})\n")
        row_models = row["models"]
        assert isinstance(row_models, list)
        for model in row_models:
            origin = "catalogued" if model["catalogue"] is not None else "uncatalogued"
            used_by = model["used_by"]
            assert isinstance(used_by, list)
            usage = ",".join(used_by) if used_by else "-"
            io.stdout.write(f"  {model['id']}  {origin}  live={model['live']}  used_by={usage}\n")
        if args.refresh:
            row_drift = row["drift"]
            assert isinstance(row_drift, list)
            for item in row_drift:
                io.stdout.write(f"  drift {item['kind']}: {item['model']} ({item['detail']})\n")
    return 0


# ── ha run ──────────────────────────────────────────────────────────────────


def _prompt(args: argparse.Namespace, io: Io) -> tuple[str | None, bool]:
    """The task text and whether stdin is a terminal (§3.9: never wait on one).

    A target whose prompt is optional -- a review, or an implement taking
    findings -- never reads stdin unless given ``-``.
    """
    is_tty = bool(getattr(io.stdin, "isatty", lambda: False)())
    if args.prompt == "-":
        return io.stdin.read(), is_tty
    if args.prompt is not None:
        return args.prompt, is_tty
    if prompt_is_optional(
        args.target, findings=args.findings_run is not None, environ=io.environ, home=io.home
    ):
        return None, is_tty
    return (None if is_tty else io.stdin.read()), is_tty


def _read_output_schema(file: Path, *, cwd: Path) -> dict[str, object]:
    """``--output-schema FILE``: one JSON object, read before anything runs (0.5.3 lot 1).

    At most :data:`~headless_agents.structured.MAX_SCHEMA_BYTES` bytes: one byte past
    the bound is read, so an oversized file is refused without being read whole.
    Whether the object is a schema a rail can take is :func:`plan`'s question.
    """
    path = file if file.is_absolute() else cwd / file
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SCHEMA_BYTES + 1)
    except OSError as exc:
        raise UsageError(
            f"--output-schema {file}: cannot be read ({exc.strerror or type(exc).__name__})"
        ) from None
    if len(raw) > MAX_SCHEMA_BYTES:
        raise UsageError(f"--output-schema {file}: larger than {MAX_SCHEMA_BYTES} bytes")
    try:
        schema = parse_json(raw.decode("utf-8"))
    except ValueError as exc:  # a UnicodeDecodeError is one too
        raise UsageError(f"--output-schema {file}: not JSON ({exc})") from None
    if not isinstance(schema, dict):
        raise UsageError(f"--output-schema {file}: not a JSON object")
    return schema


def _write_header(outcome: Outcome, branch: str) -> str:
    """What a write prints before its text (§3.9): the run id -- what ``--continue``
    takes -- the branch, the diffstat of ``change.patch`` and its path. No git: the
    patch the engine saved is read."""
    stat = read_diffstat(outcome.run_dir)
    patch = outcome.run_dir / PATCH_FILE
    # A failed ``git diff`` leaves no patch (ticket e5b93270 item 2): never print a
    # path to nothing.
    shown = (
        str(patch)
        if patch.is_file()
        else "not written (git diff failed: see the step's commit.log)"
    )
    return (
        f"run: {outcome.run_id}\nbranch: {branch}\n"
        f"diffstat: {format_diffstat(stat) if stat is not None else '-'}\n"
        f"patch: {shown}\n\n"
    )


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
    # The schema file before stdin: a refused FILE never consumes the piped task.
    output_schema = (
        _read_output_schema(args.output_schema, cwd=io.cwd)
        if args.output_schema is not None
        else None
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
            max_tokens=args.max_tokens,
        ),
        base=args.base,
        repo=args.repo,
        run_dir=args.run_dir,
        cwd=io.cwd,
        environ=io.environ,
        home=io.home,
        continue_run=args.continue_run,
        head=args.head,
        review_run=args.review_run,
        findings_run=args.findings_run,
        wait_seconds=args.wait,
        output_schema=output_schema,
    )
    outcome = execute(plan(request), say=io.say)
    report = outcome.report
    branch, verdict = report.get("branch"), report.get("verdict")
    if args.json:
        io.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    elif isinstance(verdict, str):
        # A review's verdict is its answer, APPROVE or CHANGES alike: the deciding text is
        # what a session reads, and feeds back with --findings RUN_ID (§3.9).
        io.stdout.write(f"run: {outcome.run_id}\nhead: {report.get('head')}\n\n")
        text = report.get("text")
        if isinstance(text, str) and text:
            io.stdout.write(text if text.endswith("\n") else text + "\n")
    elif outcome.final is not None:
        if isinstance(branch, str):
            io.stdout.write(_write_header(outcome, branch))
        text = outcome.final.text
        if text:
            io.stdout.write(text if text.endswith("\n") else text + "\n")
    if outcome.exit_code != 0 and not isinstance(verdict, str):
        provider = outcome.final.provider if outcome.final is not None else args.target
        reason = report.get("failure_reason")
        because = f" ({reason})" if isinstance(reason, str) else ""
        io.say(
            f"{provider} exited {outcome.exit_code}{because}; run {outcome.run_id}, "
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
            + (
                "  "
                + " / ".join(
                    "confined"
                    if isinstance(link["confinement"], str)
                    and link["confinement"].startswith("confined")
                    else "writes serialised"
                    for link in links
                )
                if row["write"]
                else ""
            )
            + (f"  instructions {row['instructions_bytes']} B" if row["instructions_bytes"] else "")
            + "\n"
        )
    return 0


# ── ha workflows ────────────────────────────────────────────────────────────


def _workflows(args: argparse.Namespace, io: Io) -> int:
    rows = describe_workflows(io.environ, io.home)
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    if not rows:
        path = config_file("workflows.toml", io.environ, home=io.home) or (
            config_dir(io.environ, home=io.home) / "workflows.toml"
        )
        io.stdout.write(f"No workflows declared in {path}.\n")
        return 0
    for row in rows:
        slots = row["slots"]
        assert isinstance(slots, list)
        grouped: dict[str, list[str]] = {}
        for slot in slots:
            providers = ", ".join(slot["providers"])
            grouped.setdefault(slot["slot"], []).append(f"{slot['role']} ({providers})")
        described = "; ".join(f"{name}: {', '.join(roles)}" for name, roles in grouped.items())
        io.stdout.write(f"{row['name']:<20} {row['shape']:<9}  {described}\n")
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


def _registered_row(
    registry: Registry, run_id: str, *, waiting_ids: frozenset[str] = frozenset()
) -> dict[str, object]:
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
    if entry.lineage is not None and run_id not in waiting_ids:
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
        else registry.effective_status(entry, lineage_status, waiting_ids=waiting_ids),
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
    waiting_ids = frozenset(w.label for w in locks.waiters(registry.state) if w.alive and w.label)
    entries: list[tuple[int, dict[str, object]]] = [
        (_admitted_at(registry, run_id), _registered_row(registry, run_id, waiting_ids=waiting_ids))
        for run_id in registered
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
    """``ha show RUN_ID``: rebuilt from the state (plan P6); 1 when part of it is unreadable.
    ``ha show --dir PATH``: a run directory's report, for display only (§3.8.6)."""
    if (args.run_id is None) == (args.dir is None):
        raise UsageError("ha show takes a run id or --dir PATH, exactly one of them")
    try:
        if args.dir is not None:
            shown = show.from_dir(Path(os.path.abspath(io.cwd / args.dir)))
        else:
            shown = show.rebuild(
                args.run_id,
                state=state_dir(io.environ, home=io.home),
                runs_root=runs_root(io.home),
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
        if args.command == "models":
            return _models(args, io)
        if args.command == "run":
            return _run(args, io)
        if args.command == "roles":
            return _roles(args, io)
        if args.command == "workflows":
            return _workflows(args, io)
        if args.command == "runs":
            return _runs(args, io)
        if args.command == "show":
            return _show(args, io)
        if args.command == "clean":
            return _clean(args, io)
        if args.command == "prove":
            return _prove(args, io)
        raise UsageError(
            "a command is required: run, roles, workflows, providers, models, runs, show, "
            "clean or prove"
        )
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
