"""The engine: one dispatch path holding every gate (spec 0.5.0 §3.4).

In 0.4.0 the rules of ``ha run`` lived in the argparse layer, and a library
caller bypassed them -- the defect red-lab paid for (f9eff72c): a gate
enforced by one entry point leaks through the other. Here every gate lives
in the engine every entry point shares, and ``cli.py`` only parses and prints.

- :func:`plan` resolves the target and applies every gate that needs neither
  git nor the mutable state -- configuration, capabilities, models, the MCP
  profile, the ``openai-compat`` endpoint, prompt presence and size, run
  directory naming -- and raises :class:`UsageError` before anything runs.
  It never runs git (§3.8.2): the repository is identified in ``execute``.
- ``execute`` (PR B, Task 14) takes the locks and runs the step.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from . import locks
from .capability import scoped_environment
from .chain import run_chain
from .cli_models import ModelsError, models_for
from .config_paths import ConfigPathError, config_file, state_dir
from .context import ContextBundle, ContextLevel, resolve_context, role_instructions
from .locks import LockTimeout, Rank, held
from .mcp_profiles import PROFILES_FILE_NAME, McpProfileError, load_profiles, mcp_server
from .profile import CapabilityProfile, Credentials, McpServer, Workspace, mcp_no_proxy_hosts
from .providers.claude import MAX_APPEND_SYSTEM_PROMPT_BYTES
from .providers.openai_compat import GENERIC_NAME
from .registry import HTTP_PROVIDER_NAMES, get_provider, max_prompt_bytes
from .repo import RepoError, discover
from .report import new_report, step_dir_name, step_entry, with_step, write_report
from .result import RunResult
from .roles import Role, RolesError, capability_rule, load_roles, resolve_role
from .run_record import RESULT_FILE_NAME
from .runs import Registry, RegistryError, make_run_dir
from .spec import RunSpec
from .workspace import prepend

ROLES_FILE_NAME: Final = "roles.toml"

#: Plan decision P5: every merged state of main keeps the spec's guarantees, so
#: write runs are refused until the write protocol of §3.8.3 is merged.
WRITE_NOT_AVAILABLE: Final = (
    "write runs are not available in this build: the write protocol of spec §3.8.3 "
    "is not merged yet"
)

# A Claude Code session that launches ``ha`` must not leak into a nested
# ``claude -p``: the child would no longer be the run the rail ships.
_PARENT_SESSION_PREFIXES: Final = ("CLAUDE_CODE_",)
_PARENT_SESSION_NAMES: Final = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_EFFORT"}
)


class UsageError(ValueError):
    """Invalid usage or configuration: exit ``2``, nothing ran."""


@dataclass(frozen=True)
class Overrides:
    """The options of ``ha run`` on a role or provider target; ``None`` = not given."""

    model: str | None = None
    effort: str | None = None
    timeout: float | None = None
    context: ContextLevel | None = None
    context_parents: bool | None = None
    mcp: str | None = None
    write: bool | None = None
    shell: bool | None = None
    base_url: str | None = None
    key_env: str | None = None


@dataclass(frozen=True)
class Request:
    target: str
    #: The task; ``None`` when absent. The CLI resolves ``-`` and a piped
    #: stdin into text; ``stdin_is_tty`` says why it may be absent.
    prompt: str | None
    stdin_is_tty: bool
    overrides: Overrides
    base: str | None
    #: Unresolved: the repository is identified in ``execute`` (plan decision P2).
    repo: Path | None
    run_dir: Path | None
    cwd: Path
    environ: Mapping[str, str]
    home: Path


@dataclass(frozen=True)
class Plan:
    request: Request
    role: Role
    models: Mapping[str, str]
    prompt: str
    mcp: McpServer | None
    environment: dict[str, str]
    run_dir: Path | None
    state: Path


def operator_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if key not in _PARENT_SESSION_NAMES and not key.startswith(_PARENT_SESSION_PREFIXES)
    }


def _declared_roles(request: Request) -> tuple[dict[str, Role], Mapping[str, object]]:
    try:
        profiles_path = config_file(PROFILES_FILE_NAME, request.environ, home=request.home)
        profiles: Mapping[str, object] = (
            load_profiles(profiles_path) if profiles_path is not None else {}
        )
        roles_path = config_file(ROLES_FILE_NAME, request.environ, home=request.home)
        return load_roles(roles_path, mcp_profiles=profiles), profiles
    except (ConfigPathError, McpProfileError, RolesError) as exc:
        raise UsageError(str(exc)) from None


def _with_overrides(role: Role, overrides: Overrides) -> Role:
    """The role as this run uses it: the operator is typing, so an option wins."""
    context = overrides.context
    if context is None:
        # A write run's default context is ``full`` (§3.1), unless the role says otherwise.
        context = "full" if overrides.write and not role.write else role.context
    return replace(
        role,
        effort=overrides.effort if overrides.effort is not None else role.effort,
        timeout=overrides.timeout if overrides.timeout is not None else role.timeout,
        context=context,
        context_parents=(
            overrides.context_parents
            if overrides.context_parents is not None
            else role.context_parents
        ),
        mcp=overrides.mcp if overrides.mcp is not None else role.mcp,
        write=overrides.write if overrides.write is not None else role.write,
        # nosec B604: ``shell`` is a role capability flag, not a subprocess argument.
        shell=overrides.shell if overrides.shell is not None else role.shell,  # nosec B604
        base_url=overrides.base_url if overrides.base_url is not None else role.base_url,
        key_env=overrides.key_env if overrides.key_env is not None else role.key_env,
    )


def _prompt(request: Request) -> str:
    if request.prompt is None or not request.prompt.strip():
        where = "a terminal" if request.stdin_is_tty else "stdin"
        raise UsageError(
            f"no prompt: pass it as an argument, or on stdin with '-' (nothing came from {where})"
        )
    return request.prompt


def _bundle(role: Role, request: Request, repository: Path | None) -> ContextBundle:
    """The context bundle a step gets: the level's files, then the role's instructions."""
    bundle = resolve_context(
        level=role.context,
        repository_root=repository,
        user_files=(request.home / ".claude" / "CLAUDE.md",),
        include_parents=role.context_parents,
    )
    if role.instructions:
        bundle = bundle.with_role(role_instructions(role.name, role.instructions))
    return bundle


def _check_prompt_size(role: Role, prompt: str, bundle: ContextBundle) -> None:
    """Refuse a prompt a link cannot carry -- every link, not only the first (§3.4).

    ``plan`` counts a lower bound (no repository files yet); ``execute`` checks
    again with the real bundle, once the repository is known.
    """
    preamble = bundle.preamble()
    for provider in role.providers:
        if provider == "claude":
            size = len(preamble.encode("utf-8"))
            if size > MAX_APPEND_SYSTEM_PROMPT_BYTES:
                raise UsageError(
                    f"claude: the preamble exceeds {MAX_APPEND_SYSTEM_PROMPT_BYTES} bytes "
                    f"({size} bytes)"
                )
            continue
        limit = max_prompt_bytes(provider)
        if limit is None:
            continue
        size = len(prepend(preamble, prompt).encode("utf-8"))
        if size > limit:
            raise UsageError(
                f"{provider}: the prompt with its context exceeds {limit} bytes ({size} bytes)"
            )


def _environment(environ: Mapping[str, str], mcp: McpServer | None) -> dict[str, str]:
    environment = operator_environment(environ)
    if mcp is not None and mcp_no_proxy_hosts(mcp):
        scoped = scoped_environment(environment, no_proxy_hosts=mcp_no_proxy_hosts(mcp))
        environment.update(
            {key: value for key, value in scoped.items() if key in ("NO_PROXY", "no_proxy")}
        )
    return environment


def plan(request: Request) -> Plan:
    """Resolve ``request`` and apply every gate that needs no git; raise :class:`UsageError`."""
    declared, profiles = _declared_roles(request)
    try:
        role = _with_overrides(resolve_role(request.target, declared), request.overrides)
    except RolesError as exc:
        raise UsageError(str(exc)) from None
    rule = capability_rule(role, profiles)
    if rule is not None:
        raise UsageError(f"{request.target}: {rule}")
    if request.base is not None and not role.write:
        raise UsageError("--base needs a write run: the role's write, or --write")
    if role.write:
        raise UsageError(WRITE_NOT_AVAILABLE)

    links = tuple((link.provider, link.model) for link in role.links)
    try:
        models = models_for(
            links,
            default=request.overrides.model or "",
            role_model=role.model,
            environ=request.environ,
            home=request.home,
        )
    except ModelsError as exc:
        raise UsageError(str(exc)) from None

    prompt = _prompt(request)
    _check_prompt_size(role, prompt, _bundle(role, request, None))

    mcp: McpServer | None = None
    if role.mcp is not None:
        try:
            mcp = mcp_server(role.mcp, environ=request.environ, home=request.home)
        except McpProfileError as exc:
            raise UsageError(str(exc)) from None

    run_dir: Path | None = None
    if request.run_dir is not None:
        run_dir = Path(os.path.abspath(request.cwd / request.run_dir))
        if not run_dir.name:
            raise UsageError(
                f"--run-dir {request.run_dir} has no name: a run is named by its directory"
            )

    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=run_dir,
        state=state_dir(request.environ, home=request.home),
    )


# ── execute ─────────────────────────────────────────────────────────────────

#: The files each HOME-isolated rail needs from the operator's HOME (relative
#: to it), as the Dream and the live suite expose them.
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
MAX_TURNS: Final = 50


def runs_root(home: Path) -> Path:
    return home / ".cache" / "ha" / "runs"


def executable_for(provider: str, home: Path) -> str | None:
    relative = DEFAULT_EXECUTABLES.get(provider)
    if relative is None:
        return None
    candidate = home / relative
    return str(candidate) if candidate.is_file() and shutil.which(provider) is None else None


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    run_id: str
    run_dir: Path
    report: Mapping[str, object]
    #: The step's final link result; ``None`` when no step ran.
    final: RunResult | None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _spec_for(
    provider: str,
    plan: Plan,
    bundle: ContextBundle,
    *,
    run_id: str,
    run_dir: Path,
    workspace: Workspace | None,
) -> RunSpec:
    http = provider in HTTP_PROVIDER_NAMES
    role = plan.role
    extra: dict[str, object] = {}
    if provider == GENERIC_NAME:
        extra = {"base_url": role.base_url, "key_env": role.key_env}
    return RunSpec(
        prompt=plan.prompt,
        name=f"ha-{run_id}",
        model=plan.models[provider],
        profile=CapabilityProfile(
            mcp=None if http else plan.mcp,
            workspace=None if http else workspace,
            credentials=Credentials(paths=DEFAULT_CREDENTIALS.get(provider, ())),
        ),
        reasoning_effort=role.effort,
        max_turns=MAX_TURNS,
        timeout_seconds=role.timeout,
        run_dir=run_dir,
        executable=executable_for(provider, plan.request.home),
        environment=plan.environment,
        context=bundle,
        extra=extra,
    )


def _run_links(
    plan: Plan,
    bundle: ContextBundle,
    *,
    run_id: str,
    step_dir: Path,
    workspace: Workspace | None,
    say: Callable[[str], None],
) -> RunResult:
    """The 0.4.0 chain, unchanged: walk the role's links on 3 and 4 (§3.4)."""
    providers = plan.role.providers
    chained = len(providers) > 1
    results: dict[str, RunResult] = {}

    def link_dir(provider: str) -> Path:
        return (
            step_dir / "links" / f"{providers.index(provider)}-{provider}" if chained else step_dir
        )

    def run_one(provider: str) -> int:
        spec = _spec_for(
            provider, plan, bundle, run_id=run_id, run_dir=link_dir(provider), workspace=workspace
        )
        result = get_provider(provider).run(spec)
        results[provider] = result
        return result.exit_code

    def on_fallback(provider: str, next_provider: str) -> None:
        code = results[provider].exit_code
        say(f"{provider} exited {code} (nothing written); falling back to {next_provider}")

    outcome = run_chain(providers, run_one=run_one, on_fallback=on_fallback)
    final = results[outcome.provider]
    if chained:
        source = link_dir(outcome.provider) / RESULT_FILE_NAME
        if source.is_file():
            shutil.copyfile(source, step_dir / RESULT_FILE_NAME)
    if outcome.dead_links:
        say(f"no answer within the deadline from: {', '.join(outcome.dead_links)}")
    return final


def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
    """Run a planned one-step read-only run under its locks, and record it.

    In order: identify the repository from the filesystem (no git, plan
    decision P2); create and register the run; hold its lifecycle lock, then
    the unconfined lock shared (§3.8.2); build the real context bundle and
    check the prompt size again; run the role's chain in ``steps/01-run-<role>``;
    write ``run.json`` and the registry status. An interruption propagates
    with every lock released and the status left non-final, so the run reads
    ``incomplete``.
    """
    request, role = plan.request, plan.role
    start = request.repo.resolve() if request.repo is not None else request.cwd.resolve()
    try:
        identity = discover(start)
    except RepoError as exc:
        raise UsageError(str(exc)) from None
    repository = identity.work_tree if identity is not None else start

    runs = runs_root(request.home)
    registry = Registry(plan.state, runs_root=runs)
    if plan.run_dir is not None:
        forbidden = {"the state directory": plan.state, "the runs directory": runs}
        if identity is not None:
            forbidden["the repository"] = identity.work_tree
        try:
            make_run_dir(plan.run_dir, forbidden=forbidden)
        except RegistryError as exc:
            raise UsageError(str(exc)) from None
    target = {"kind": "provider" if role.implicit else "role", "name": role.name}
    entry = registry.register(
        run_dir=plan.run_dir, target=target, repository=repository, lineage=None
    )
    if plan.run_dir is None:
        entry.run_dir.mkdir(parents=True, mode=0o700)

    with ExitStack() as held_locks:
        held_locks.enter_context(
            held(
                registry.lifecycle_lock(entry.run_id),
                rank=Rank.LIFECYCLE,
                exclusive=True,
                wait=None,
                what=f"the lifecycle lock of {entry.run_id}",
            )
        )
        try:
            held_locks.enter_context(
                held(
                    plan.state / "unconfined.lock",
                    rank=Rank.UNCONFINED,
                    exclusive=False,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the unconfined lock",
                )
            )
        except LockTimeout:
            registry.set_status(entry.run_id, "failed")
            raise UsageError(
                "an unconfined write is running: nothing ran; retry once it has ended"
            ) from None

        bundle = _bundle(role, request, identity.work_tree if identity is not None else None)
        try:
            _check_prompt_size(role, plan.prompt, bundle)
        except UsageError:
            registry.set_status(entry.run_id, "failed")
            raise

        started = time.monotonic()
        run_dir = entry.run_dir
        (run_dir / "prompt.md").write_text(plan.prompt, encoding="utf-8")
        report = new_report(
            run_id=entry.run_id,
            target=target,
            repository=repository,
            pid=os.getpid(),
            started_at=_utc_now(),
        )
        write_report(run_dir, report)
        step_name = step_dir_name(1, "run", role.name)
        step_dir = run_dir / "steps" / step_name
        say(f"step 1 run {role.name}: started")
        final = _run_links(
            plan,
            bundle,
            run_id=entry.run_id,
            step_dir=step_dir,
            workspace=Workspace(path=repository),
            say=say,
        )
        say(f"step 1 run {role.name}: exit {final.exit_code}")
        report = with_step(
            report,
            step_entry(
                index=1, slot="run", role=role.name, step_dir=f"steps/{step_name}", result=final
            ),
        )
        status = "answered" if final.exit_code == 0 else "failed"
        report.update(
            status=status,
            exit_code=final.exit_code,
            text=final.text,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        write_report(run_dir, report)
        registry.set_status(entry.run_id, status)
        return Outcome(
            exit_code=final.exit_code,
            run_id=entry.run_id,
            run_dir=run_dir,
            report=report,
            final=final,
        )


__all__ = [
    "DEFAULT_CREDENTIALS",
    "DEFAULT_EXECUTABLES",
    "Outcome",
    "execute",
    "executable_for",
    "runs_root",
    "WRITE_NOT_AVAILABLE",
    "Overrides",
    "Plan",
    "Request",
    "UsageError",
    "operator_environment",
    "plan",
]
