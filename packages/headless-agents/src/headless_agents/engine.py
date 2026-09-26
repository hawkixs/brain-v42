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
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from . import locks, procgroup, review_flow, reviews, write_flow
from .capability import scoped_environment
from .chain import run_chain
from .cli_models import ModelsError, models_for
from .config_paths import ConfigPathError, config_file, state_dir
from .context import ContextBundle, ContextLevel, resolve_context, role_instructions
from .locks import LockTimeout, Rank, held
from .mcp_profiles import PROFILES_FILE_NAME, McpProfileError, load_profiles, mcp_server
from .profile import CapabilityProfile, Credentials, McpServer, Workspace, mcp_no_proxy_hosts
from .proofs import CLI_RAILS, confinement, isolation_label, isolation_ok
from .providers.claude import MAX_APPEND_SYSTEM_PROMPT_BYTES
from .providers.openai_compat import GENERIC_NAME
from .registry import (
    HTTP_PROVIDER_NAMES,
    PROVIDER_NAMES,
    get_provider,
    max_prompt_bytes,
    probe,
    tool_counts,
)
from .repo import RepoError, RepoIdentity, discover
from .report import (
    PROMPT_FILE,
    RUN_JSON,
    new_report,
    refused_step_entry,
    step_dir_name,
    step_entry,
    with_step,
    write_report,
)
from .result import RunResult
from .roles import Role, RolesError, capability_rule, load_roles, resolve_role
from .run_record import RESULT_FILE_NAME
from .runs import MINT_ATTEMPTS, Entry, Registry, RegistryError, make_run_dir
from .spec import RunSpec
from .state import Unknown
from .templates import (
    ReviewText,
    Verdict,
    fix_prompt,
    implement_prompt,
    judge_prompt,
    read_verdict,
    review_prompt,
)
from .workflows import Workflow, WorkflowsError, load_workflows
from .workspace import prepend

ROLES_FILE_NAME: Final = "roles.toml"
WORKFLOWS_FILE_NAME: Final = "workflows.toml"

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
    #: ``--continue RUN_ID``: an implement run whose lineage this run joins (§3.6).
    continue_run: str | None = None
    #: ``--head REF`` of a review (§3.5); ``None`` means ``HEAD``.
    head: str | None = None
    #: ``--run RUN_ID`` of a review: an implement run whose lineage's tip is reviewed.
    review_run: str | None = None
    #: ``--findings RUN_ID`` of an implement run: a review whose verdict was read (§3.6).
    findings_run: str | None = None


@dataclass(frozen=True)
class SlotPlan:
    """One slot of a review's panel, planned like a role run (§3.5)."""

    slot: str
    role: Role
    models: Mapping[str, str]
    mcp: McpServer | None
    environment: dict[str, str]


@dataclass(frozen=True)
class Plan:
    request: Request
    role: Role
    models: Mapping[str, str]
    #: What the provider gets: the task as given, or a template around it (§3.7).
    prompt: str
    mcp: McpServer | None
    environment: dict[str, str]
    run_dir: Path | None
    state: Path
    #: The workflow a workflow target names; ``None`` for a role or a provider.
    workflow: Workflow | None = None
    #: The task as given, written to ``prompt.md`` (§3.10); ``None`` means ``prompt``.
    task: str | None = None
    #: The run ``--continue`` names, and the owner of the lineage it joins (§3.6),
    #: both read from the registry; ``execute`` checks them again under the lock.
    continues: str | None = None
    joins: str | None = None
    #: A review's panel -- its reviewers, then its judge -- in launch order.
    panel: tuple[SlotPlan, ...] = ()
    #: The run ``--run`` names, and the owner of its lineage, read from the registry.
    reviews: str | None = None
    reviewed_lineage: str | None = None
    #: The review ``--findings`` names, read from the registry; its result is read by
    #: ``execute``, which composes the fix prompt from it (§3.6).
    findings_from: str | None = None


def operator_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if key not in _PARENT_SESSION_NAMES and not key.startswith(_PARENT_SESSION_PREFIXES)
    }


def declared_roles(
    environ: Mapping[str, str], home: Path
) -> tuple[dict[str, Role], Mapping[str, object]]:
    """The roles of ``roles.toml`` and the profiles of ``mcp.toml``, validated."""
    try:
        profiles_path = config_file(PROFILES_FILE_NAME, environ, home=home)
        profiles: Mapping[str, object] = (
            load_profiles(profiles_path) if profiles_path is not None else {}
        )
        roles_path = config_file(ROLES_FILE_NAME, environ, home=home)
        return load_roles(roles_path, mcp_profiles=profiles), profiles
    except (ConfigPathError, McpProfileError, RolesError) as exc:
        raise UsageError(str(exc)) from None


@dataclass(frozen=True)
class Config:
    """The operator's configuration, validated as a whole before anything runs (§3.4)."""

    roles: dict[str, Role]
    profiles: Mapping[str, object]
    workflows: dict[str, Workflow]


def load_config(environ: Mapping[str, str], home: Path) -> Config:
    """``roles.toml``, ``mcp.toml`` and ``workflows.toml``, validated; ``models.toml``
    is read per link, when a run resolves its models.

    Every file comes from the operator's configuration directory only (§3.3),
    and an invalid one refuses every run, whatever its target: validation
    happens before anything runs (§3.1, §3.2).
    """
    roles, profiles = declared_roles(environ, home)
    try:
        path = config_file(WORKFLOWS_FILE_NAME, environ, home=home)
        workflows = load_workflows(path, roles=roles)
    except (ConfigPathError, WorkflowsError) as exc:
        raise UsageError(str(exc)) from None
    return Config(roles=roles, profiles=profiles, workflows=workflows)


def describe_workflows(environ: Mapping[str, str], home: Path) -> list[dict[str, object]]:
    """``ha workflows``: every declared workflow, its shape and its slots, each slot's role
    with the providers of every link of that role (spec §3.9)."""
    config = load_config(environ, home)
    rows: list[dict[str, object]] = []
    for workflow in config.workflows.values():
        slots = [
            {
                "slot": slot,
                "role": name,
                "providers": list(resolve_role(name, config.roles).providers),
            }
            for slot, name in workflow.slot_roles()
        ]
        rows.append({"name": workflow.name, "shape": workflow.shape, "slots": slots})
    return rows


def describe_roles(environ: Mapping[str, str], home: Path) -> list[dict[str, object]]:
    """``ha roles``: every declared role, resolved (spec §3.9)."""
    declared, _ = declared_roles(environ, home)
    rows: list[dict[str, object]] = []
    for role in declared.values():
        links: list[dict[str, object]] = []
        for link in role.links:
            try:
                model: str | None = models_for(
                    ((link.provider, link.model),),
                    default="",
                    role_model=role.model,
                    environ=environ,
                    home=home,
                )[link.provider]
            except ModelsError:
                model = None
            version = (
                _installed_version(link.provider, home, environ)
                if link.provider in CLI_RAILS
                else None
            )
            state = state_dir(environ, home=home)
            confined: str | None = None
            if role.write:
                label, date = confinement(state, link.provider, version)
                if role.shell and link.provider in _UNSANDBOXED_SHELL_RAILS:
                    label, date = "unconfined", None
                confined = f"{label} ({date})" if label == "confined" else label
            links.append(
                {
                    "provider": link.provider,
                    "model": model,
                    "isolation": isolation_label(state, link.provider, version),
                    "confinement": confined,
                }
            )
        rows.append(
            {
                "name": role.name,
                "links": links,
                "effort": role.effort,
                "timeout": role.timeout,
                "context": role.context,
                "context_parents": role.context_parents,
                "mcp": role.mcp,
                "write": role.write,
                "shell": role.shell,
                "instructions_bytes": len(role.instructions.encode("utf-8"))
                if role.instructions
                else 0,
            }
        )
    return rows


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


def _models(role: Role, request: Request) -> Mapping[str, str]:
    links = tuple((link.provider, link.model) for link in role.links)
    try:
        return models_for(
            links,
            default=request.overrides.model or "",
            role_model=role.model,
            environ=request.environ,
            home=request.home,
        )
    except ModelsError as exc:
        raise UsageError(str(exc)) from None


def _mcp(role: Role, request: Request) -> McpServer | None:
    if role.mcp is None:
        return None
    try:
        return mcp_server(role.mcp, environ=request.environ, home=request.home)
    except McpProfileError as exc:
        raise UsageError(str(exc)) from None


def _run_dir(request: Request) -> Path | None:
    if request.run_dir is None:
        return None
    run_dir = Path(os.path.abspath(request.cwd / request.run_dir))
    if not run_dir.name:
        raise UsageError(
            f"--run-dir {request.run_dir} has no name: a run is named by its directory"
        )
    return run_dir


#: The ``ha run`` options that override a role (§3.9), by their ``Overrides`` field.
_OVERRIDE_FLAGS: Final[Mapping[str, str]] = {
    "model": "-m",
    "effort": "--effort",
    "timeout": "--timeout",
    "context": "--context",
    "context_parents": "--context-parents",
    "mcp": "--mcp",
    "write": "--write",
    "shell": "--shell",
    "base_url": "--base-url",
    "key_env": "--key-env",
}


def _continued_lineage(run_id: str, *, state: Path, home: Path, option: str = "--continue") -> str:
    """The owner of the implement lineage ``--continue`` or ``--run`` names, from the registry.

    No git and no lineage state here (§3.8.2): only the run's registry entry,
    which must name an ``implement`` run -- a member of an ``implement``
    lineage (§3.5, §3.6). ``execute`` checks it again, then the lineage itself,
    under the lineage lock.
    """
    registry = Registry(state, runs_root=runs_root(home))
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        raise UsageError(f"{option}: {exc}") from None
    except Unknown as exc:
        raise UsageError(f"{option} {run_id}: {exc}; recover it by hand") from None
    target = entry.target
    if (
        entry.lineage is None
        or target.get("kind") != "workflow"
        or target.get("shape") != "implement"
    ):
        verb = "continued" if option == "--continue" else "reviewed with --run"
        raise UsageError(
            f"{option} {run_id}: not an implement run; only an implement run's lineage "
            f"can be {verb}"
        )
    return entry.lineage


#: A review's task when none is given (§3.5): its prompt is optional.
REVIEW_DEFAULT_TASK: Final = "Review this change."


def prompt_is_optional(
    target: str, *, findings: bool, environ: Mapping[str, str], home: Path
) -> bool:
    """Whether ``target`` runs without a prompt: a review, or an implement taking findings.

    §3.9: such a target never reads stdin unless given ``-``. The CLI asks before
    reading; the configuration is read here, and an invalid one refuses (exit ``2``),
    as :func:`plan` would.
    """
    if findings:
        # Whatever the target: one that cannot take --findings is refused by plan(),
        # and stdin is never read on the way there.
        return True
    workflow = load_config(environ, home).workflows.get(target)
    return workflow is not None and workflow.shape == "review"


def _refuse_options_of_other_shapes(request: Request, shape: str | None) -> None:
    """Spec §3.9: ``--head`` and ``--run`` belong to a review; ``--continue`` to an implement."""
    if shape != "review":
        for flag, value in (("--head", request.head), ("--run", request.review_run)):
            if value is not None:
                raise UsageError(f"{flag} needs a review workflow as the target")
    if shape != "implement":
        for flag, value in (
            ("--continue", request.continue_run),
            ("--findings", request.findings_run),
        ):
            if value is not None:
                raise UsageError(f"{flag} needs an implement workflow as the target")


def _findings_review(run_id: str, *, state: Path, home: Path) -> str:
    """The review ``--findings`` names, from the registry only (§3.8.2): a review run."""
    registry = Registry(state, runs_root=runs_root(home))
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        raise UsageError(f"--findings: {exc}") from None
    except Unknown as exc:
        raise UsageError(f"--findings {run_id}: {exc}; recover it by hand") from None
    target = entry.target
    if (
        entry.lineage is not None
        or target.get("kind") != "workflow"
        or target.get("shape") != "review"
    ):
        raise UsageError(f"--findings {run_id}: not a review run; findings come from a review")
    return run_id


def _findings(plan: Plan) -> reviews.ReviewResult | None:
    """The result of the review ``--findings`` names, read from the state (§3.6), never its
    report -- so it still reads after ``ha clean`` of that review."""
    if plan.findings_from is None:
        return None
    request = plan.request
    _findings_review(plan.findings_from, state=plan.state, home=request.home)
    try:
        return reviews.load_result(plan.state, plan.findings_from)
    except Unknown as exc:
        raise UsageError(
            f"--findings {plan.findings_from}: the review result is missing or unknown ({exc}): "
            "only a review whose verdict was read has findings"
        ) from None


def _slot_plan(slot: str, role: Role, request: Request, config: Config, name: str) -> SlotPlan:
    rule = capability_rule(role, config.profiles)
    if rule is not None:
        # Validated when workflows.toml was read; the engine checks again (§3.4).
        raise UsageError(f"{name}: {rule}")
    mcp = _mcp(role, request)
    return SlotPlan(
        slot=slot,
        role=role,
        models=_models(role, request),
        mcp=mcp,
        environment=_environment(request.environ, mcp),
    )


def _plan_review(request: Request, workflow: Workflow, config: Config) -> Plan:
    """A review target: every slot planned; the head, base and diff wait for ``execute``.

    The prompt is optional (§3.9) -- absent, it reviews the change -- and its
    size is checked when each step starts, once the diff is known (§3.4).
    ``--run`` is resolved through the registry only (§3.8.2).
    """
    if request.review_run is not None and (request.head is not None or request.base is not None):
        raise UsageError(
            "--run excludes --head and --base: it stands for the lineage's tip and its base"
        )
    panel = tuple(
        _slot_plan(slot, resolve_role(name, config.roles), request, config, workflow.name)
        for slot, name in workflow.slot_roles()
    )
    state = state_dir(request.environ, home=request.home)
    lineage = (
        _continued_lineage(request.review_run, state=state, home=request.home, option="--run")
        if request.review_run is not None
        else None
    )
    task = (request.prompt or "").strip() or REVIEW_DEFAULT_TASK
    first = panel[0]
    return Plan(
        request=request,
        role=first.role,
        models=first.models,
        prompt=task,
        mcp=first.mcp,
        environment=first.environment,
        run_dir=_run_dir(request),
        state=state,
        workflow=workflow,
        task=task,
        panel=panel,
        reviews=request.review_run,
        reviewed_lineage=lineage,
    )


def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan:
    """A workflow target: its roles run as declared (§3.3), its prompt is a template (§3.7)."""
    given = [
        flag
        for field, flag in _OVERRIDE_FLAGS.items()
        if getattr(request.overrides, field) is not None
    ]
    if given:
        own = "--head and --run" if workflow.shape == "review" else "--continue"
        raise UsageError(
            f"workflow {workflow.name}: a workflow runs its roles as declared, so "
            f"{', '.join(given)} is refused; its options are --base, --repo, --json, --run-dir "
            f"and {own}"
        )
    _refuse_options_of_other_shapes(request, workflow.shape)
    if workflow.implement is None:
        return _plan_review(request, workflow, config)
    if request.continue_run is not None and request.base is not None:
        raise UsageError(
            "--base and --continue exclude each other: a continuation works from its lineage's base"
        )
    role = resolve_role(workflow.implement, config.roles)
    # Validated when workflows.toml was read; the engine checks again (§3.4).
    rule = capability_rule(role, config.profiles)
    if rule is not None:
        raise UsageError(f"{workflow.name}: {rule}")
    models = _models(role, request)
    state = state_dir(request.environ, home=request.home)
    findings_from = (
        _findings_review(request.findings_run, state=state, home=request.home)
        if request.findings_run is not None
        else None
    )
    if findings_from is not None:
        # §3.9: with --findings the task is optional guidance; stdin is never read.
        task = (request.prompt or "").strip()
        prompt = fix_prompt(task, "")
    else:
        task = _prompt(request)
        prompt = implement_prompt(task)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    run_dir = _run_dir(request)
    joins = (
        _continued_lineage(request.continue_run, state=state, home=request.home)
        if request.continue_run is not None
        else None
    )
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=run_dir,
        state=state,
        workflow=workflow,
        task=task,
        continues=request.continue_run,
        joins=joins,
        findings_from=findings_from,
    )


def plan(request: Request) -> Plan:
    """Resolve ``request`` and apply every gate that needs no git; raise :class:`UsageError`."""
    config = load_config(request.environ, request.home)
    workflow = config.workflows.get(request.target)
    if workflow is not None:
        return _plan_workflow(request, workflow, config)
    _refuse_options_of_other_shapes(request, None)
    declared, profiles = config.roles, config.profiles
    if request.target not in declared and request.target not in PROVIDER_NAMES:
        known = sorted({*config.workflows, *declared, *PROVIDER_NAMES})
        raise UsageError(
            f"unknown target {request.target!r}; workflows, roles and providers: {', '.join(known)}"
        )
    role = _with_overrides(resolve_role(request.target, declared), request.overrides)
    rule = capability_rule(role, profiles)
    if rule is not None:
        raise UsageError(f"{request.target}: {rule}")
    if request.base is not None and not role.write:
        raise UsageError("--base needs a write run: the role's write, or --write")

    models = _models(role, request)
    prompt = _prompt(request)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=_run_dir(request),
        state=state_dir(request.environ, home=request.home),
        task=prompt,
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


def _installed_version(provider: str, home: Path, environ: Mapping[str, str]) -> str | None:
    return probe(provider, executable=executable_for(provider, home), environ=environ).version


def _check_isolation(plan: Plan) -> None:
    """Refuse a CLI rail without a passing isolation proof for its version (§3.8.0).

    Every link is checked before any runs: fault tolerance must not route a run
    onto an unproven executor.
    """
    for provider in plan.role.providers:
        if provider not in CLI_RAILS:
            continue
        version = _installed_version(provider, plan.request.home, plan.environment)
        if isolation_ok(plan.state, provider, version):
            continue
        label = isolation_label(plan.state, provider, version)
        raise UsageError(
            f"{provider} {version or '(version unknown)'} has no passing isolation proof "
            f"({label}): a rail that may load the operator's configuration is refused. "
            f"Record a proof with: HA_LIVE=1 pytest -m live "
            f'tests/live/headless_agents/test_proofs_live.py -k "isolation and {provider}"'
        )


def _admit(
    registry: Registry,
    held_locks: ExitStack,
    *,
    run_dir: Path | None,
    target: Mapping[str, str],
    repository: Path,
    write: bool = False,
    joins: str | None = None,
    continues: str | None = None,
    providers: Sequence[str] = (),
    findings_from: str | None = None,
) -> Entry:
    """Mint an id, take its lifecycle lock, then publish its entry (§3.8.3 step 1).

    A write run's entry names its lineage -- the one it starts, owned by its
    own id, or the one it ``joins`` as a continuation -- so its status lives in
    the lineage state only (§3.8.1). ``continues`` and ``providers`` are the
    continuation records the report copies (§3.10).

    The lock comes first: an entry is never visible with a free lock before
    its run starts, so ``ha clean`` cannot forget a run that is being admitted
    (codex review of #207, round 2). An id whose lock another process holds,
    or whose entry exists, is taken: mint again. The lock stays on
    ``held_locks`` until the run ends.
    """
    last: LockTimeout | None = None
    for _ in range(MINT_ATTEMPTS):
        run_id = registry.mint()
        with ExitStack() as attempt:
            try:
                attempt.enter_context(
                    held(
                        registry.lifecycle_lock(run_id),
                        rank=Rank.LIFECYCLE,
                        exclusive=True,
                        wait=None,
                        what=f"the lifecycle lock of {run_id}",
                    )
                )
            except LockTimeout as exc:
                last = exc
                continue
            try:
                entry = registry.create(
                    run_id,
                    run_dir=run_dir,
                    target=target,
                    repository=repository,
                    lineage=joins if joins is not None else (run_id if write else None),
                    continues=continues,
                    providers=providers,
                    findings_from=findings_from,
                )
            except FileExistsError:
                continue
            held_locks.push(attempt.pop_all())
            return entry
    if last is not None:
        raise last
    raise RegistryError(f"could not mint a fresh run id in {MINT_ATTEMPTS} attempts")


#: Rails whose shell runs outside any sandbox of theirs: a ``shell`` write role
#: on one of them is unconfined whatever its proofs say (decision 13; codex
#: keeps its sandboxed shell).
_UNSANDBOXED_SHELL_RAILS: Final = frozenset({"claude", "opencode", "agy"})


def write_is_unconfined(plan: Plan) -> bool:
    """Whether a write takes the unconfined path of §3.8.3 (plan Tasks 20, 22).

    Any link on a rail without a passing confinement proof for its installed
    version, whatever the rail, codex included; or a ``shell`` role on a rail
    whose shell is unsandboxed. One unconfined link sends the whole write down
    the unconfined path: it holds the unconfined lock exclusively, publishes
    its intent, and has its worktree's ``HEAD`` reflog read for agent commits.
    """
    role = plan.role
    if role.shell and any(provider in _UNSANDBOXED_SHELL_RAILS for provider in role.providers):
        return True
    for provider in role.providers:
        if provider not in CLI_RAILS:
            continue
        version = _installed_version(provider, plan.request.home, plan.environment)
        if confinement(plan.state, provider, version)[0] != "confined":
            return True
    return False


def _refused(registry: Registry, entry: Entry) -> None:
    """A gate refused the run before its step: a read-only run is ``failed``; a
    write run never started, so its entry and directory go (§3.8.1)."""
    if entry.lineage is None:
        registry.set_status(entry.run_id, "failed")
        return
    shutil.rmtree(entry.run_dir, ignore_errors=True)
    registry.forget(entry.run_id)


def _execute_write(
    plan: Plan,
    bundle: ContextBundle,
    *,
    registry: Registry,
    entry: Entry,
    identity: RepoIdentity,
    start: Path,
    report: dict[str, object],
    slot: str,
    step_name: str,
    started: float,
    unconfined: bool,
    say: Callable[[str], None],
    findings_head: str | None = None,
) -> Outcome:
    """A write run -- a role's, or an ``implement`` workflow's: §3.8.3, then its report."""
    role, run_dir = plan.role, entry.run_dir
    step_dir = run_dir / "steps" / step_name

    def run_links(workspace: Workspace, directory: Path) -> RunResult:
        say(f"step 1 {slot} {role.name}: started")
        final = _run_links(
            plan, bundle, run_id=entry.run_id, step_dir=directory, workspace=workspace, say=say
        )
        say(f"step 1 {slot} {role.name}: exit {final.exit_code}")
        return final

    try:
        outcome = write_flow.run_write_step(
            plan,
            run_id=entry.run_id,
            run_dir=run_dir,
            state=plan.state,
            identity=identity,
            start=start,
            step_dir=step_dir,
            run_links=run_links,
            say=say,
            unconfined=unconfined,
            joins=plan.joins,
            named=plan.continues,
            findings_head=findings_head,
        )
    except write_flow.WriteRefused as exc:
        _refused(registry, entry)
        raise UsageError(str(exc)) from None
    if outcome.final is not None:
        report = with_step(
            report,
            step_entry(
                index=1,
                slot=slot,
                role=role.name,
                step_dir=f"steps/{step_name}",
                result=outcome.final,
                tools=tool_counts(outcome.final),
            ),
        )
    report.update(
        status=outcome.status,
        exit_code=outcome.exit_code,
        text=outcome.final.text if outcome.final is not None else None,
        branch=outcome.branch,
        base=outcome.base,
        head=outcome.head,
        lineage=entry.lineage,
        continues=entry.continues,
        findings_from=entry.findings_from,
        implement_providers=list(entry.providers),
        commits=[{"sha": sha, "made_by": made_by} for sha, made_by in outcome.commits],
        failure_reason=outcome.failure_reason,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    write_report(run_dir, report)
    return Outcome(
        exit_code=outcome.exit_code,
        run_id=entry.run_id,
        run_dir=run_dir,
        report=report,
        final=outcome.final,
    )


def _slot_run_plan(plan: Plan, slot: SlotPlan, prompt: str) -> Plan:
    """The plan of one panel step: its own role, models, MCP and environment (§3.5)."""
    return replace(
        plan,
        role=slot.role,
        models=slot.models,
        mcp=slot.mcp,
        environment=slot.environment,
        prompt=prompt,
    )


@dataclass(frozen=True)
class _Phase:
    """A phase's steps once they ran: entries for the report, and the texts."""

    entries: list[dict[str, object]]
    results: list[RunResult | None]
    failure_reason: str | None


def _run_phase(
    plan: Plan,
    steps: Sequence[tuple[int, SlotPlan, str]],
    *,
    run_id: str,
    run_dir: Path,
    worktree: Path,
    say: Callable[[str], None],
) -> _Phase:
    """Run ``(index, slot, prompt)`` steps in parallel, each read-only on the worktree.

    Every prompt is size-checked first, now that the diff is known (§3.4): one
    too large refuses the whole phase before anything starts, its step recorded
    with code ``2``. Each step must answer -- exit ``0`` with a non-empty text --
    or the phase fails (§3.5 step 3).
    """
    bundles = {index: _bundle(slot.role, plan.request, worktree) for index, slot, _ in steps}
    for index, slot, prompt in steps:
        try:
            _check_prompt_size(slot.role, prompt, bundles[index])
        except UsageError as exc:
            say(f"step {index} {slot.slot} {slot.role.name}: refused ({exc})")
            name = step_dir_name(index, slot.slot, slot.role.name)
            entry = refused_step_entry(
                index=index, slot=slot.slot, role=slot.role.name, step_dir=f"steps/{name}"
            )
            return _Phase(entries=[entry], results=[None], failure_reason="prompt_too_large")

    # A library caller's say need not be thread-safe: the steps take turns.
    said_lock = threading.Lock()

    def said(message: str) -> None:
        with said_lock:
            say(message)

    live: set[subprocess.Popen[Any]] = set()

    def run_one(step: tuple[int, SlotPlan, str]) -> RunResult:
        index, slot, prompt = step
        name = step_dir_name(index, slot.slot, slot.role.name)
        said(f"step {index} {slot.slot} {slot.role.name}: started")
        with procgroup.collecting(live):
            final = _run_links(
                _slot_run_plan(plan, slot, prompt),
                bundles[index],
                run_id=run_id,
                step_dir=run_dir / "steps" / name,
                workspace=Workspace(path=worktree),
                say=said,
            )
        said(f"step {index} {slot.slot} {slot.role.name}: exit {final.exit_code}")
        return final

    pool = ThreadPoolExecutor(max_workers=len(steps))
    futures = [pool.submit(run_one, step) for step in steps]
    try:
        results = [future.result() for future in futures]
    except BaseException:
        # A Ctrl-C lands here, in the main thread; the rails wait in the workers and
        # never see it. Their providers die first, then their threads are joined --
        # only then may the caller remove the worktree they were reading (§3.9).
        procgroup.kill_collected(live)
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    entries = []
    failed = False
    for (index, slot, _), result in zip(steps, results, strict=True):
        name = step_dir_name(index, slot.slot, slot.role.name)
        entry = step_entry(
            index=index,
            slot=slot.slot,
            role=slot.role.name,
            step_dir=f"steps/{name}",
            result=result,
            tools=tool_counts(result),
        )
        entry["verdict"] = read_verdict(result.text)
        entries.append(entry)
        failed = failed or result.exit_code != 0 or not (result.text or "").strip()
    return _Phase(
        entries=entries, results=list(results), failure_reason="step_failed" if failed else None
    )


def _execute_review(
    plan: Plan,
    *,
    registry: Registry,
    entry: Entry,
    identity: RepoIdentity,
    start: Path,
    report: dict[str, object],
    started: float,
    say: Callable[[str], None],
) -> Outcome:
    """A review run: the vendor rule and the pinned change, the reviewers in parallel,
    then the judge; the verdict, the result and the cleanup (§3.5, §3.8.4)."""
    request, run_dir = plan.request, entry.run_dir
    reviewers = [(i, s) for i, s in enumerate(plan.panel, 1) if s.slot == "review"]
    judge = next(((i, s) for i, s in enumerate(plan.panel, 1) if s.slot == "judge"), None)
    try:
        prepared = review_flow.prepare(
            run_id=entry.run_id,
            run_dir=run_dir,
            state=plan.state,
            identity=identity,
            start=start,
            environ=plan.environment,
            head_ref=request.head,
            base_ref=request.base,
            reviewed_lineage=plan.reviewed_lineage,
            reviewers={slot.role.name: slot.role.providers for _, slot in reviewers},
        )
    except review_flow.ReviewRefused as exc:
        _refused(registry, entry)
        raise UsageError(str(exc)) from None
    task = plan.task or REVIEW_DEFAULT_TASK
    try:
        report.update(
            head=prepared.head,
            base=prepared.merge_base,
            vendor_check=prepared.check.to_document(),
        )
        write_report(run_dir, report)
        phase = _run_phase(
            plan,
            [(index, slot, review_prompt(task, prepared.patch)) for index, slot in reviewers],
            run_id=entry.run_id,
            run_dir=run_dir,
            worktree=prepared.worktree,
            say=say,
        )
        entries, failure = list(phase.entries), phase.failure_reason
        deciding: RunResult | None = phase.results[0] if len(phase.results) == 1 else None
        if failure is None and judge is not None:
            index, slot = judge
            answers = [
                ReviewText(
                    role=slot_.role.name,
                    provider=result.provider,
                    model=result.model_reported or result.model or "",
                    text=result.text or "",
                )
                for (_, slot_), result in zip(reviewers, phase.results, strict=True)
                if result is not None
            ]
            judged = _run_phase(
                plan,
                [(index, slot, judge_prompt(task, prepared.patch, answers))],
                run_id=entry.run_id,
                run_dir=run_dir,
                worktree=prepared.worktree,
                say=say,
            )
            entries.extend(judged.entries)
            failure = judged.failure_reason
            deciding = judged.results[0]
        verdict: Verdict | None = None
        text = deciding.text if deciding is not None else None
        if failure is None:
            verdict = read_verdict(text)
            if verdict is None:
                failure = "unreadable_verdict"
    except BaseException:
        # A crash or an interruption after the worktree exists: remove it, then let the
        # run read incomplete, as any interrupted run does -- no verdict, no result.
        kept = review_flow.remove_worktree(
            prepared.worktree, identity, plan.environment, plan.state
        )
        if kept is not None:
            say(f"the review's worktree was kept ({kept}): ha clean retries")
        raise
    cleanup = review_flow.finish(
        run_id=entry.run_id,
        state=plan.state,
        identity=identity,
        environ=plan.environment,
        prepared=prepared,
        verdict=verdict,
        text=text,
    )
    if cleanup["status"] != "done":
        say(f"the review's worktree was kept ({cleanup.get('reason')}): ha clean retries")
    if verdict == "approve":
        status, code = "approved", 0
    elif verdict == "changes":
        status, code = "changes", review_flow.CHANGES_EXIT_CODE
    else:
        status, code = "failed", 1
    for step in entries:
        report = with_step(report, step)
    report.update(
        status=status,
        exit_code=code,
        verdict=verdict,
        text=text,
        failure_reason=failure,
        cleanup=cleanup,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    write_report(run_dir, report)
    registry.set_status(entry.run_id, status)
    return Outcome(
        exit_code=code, run_id=entry.run_id, run_dir=run_dir, report=report, final=deciding
    )


def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
    """Run a planned one-step run -- a role's, or an ``implement`` workflow's -- under its
    locks, and record it.

    In order: identify the repository from the filesystem (no git, plan
    decision P2); mint the run, hold its lifecycle lock, then register it; take
    the unconfined lock shared (§3.8.2); build the real context bundle and
    check the prompt size again; run the role's chain in
    ``steps/01-<slot>-<role>``; write ``run.json`` and the registry status. An
    interruption propagates with every lock released and the status left
    non-final, so the run reads ``incomplete``.
    """
    request, role = plan.request, plan.role
    start = request.repo.resolve() if request.repo is not None else request.cwd.resolve()
    try:
        identity = discover(start)
    except RepoError as exc:
        raise UsageError(str(exc)) from None
    repository = identity.work_tree if identity is not None else start
    if role.write and identity is None:
        raise UsageError(f"a write run needs a git repository; {start} is not in one")
    if plan.panel and identity is None:
        raise UsageError(f"a review needs a git repository; {start} is not in one")
    if plan.continues is not None:
        # §3.8.2: what plan() read from the registry is read again, before anything
        # is created; the lineage itself is checked under its lock (write_flow).
        if _continued_lineage(plan.continues, state=plan.state, home=request.home) != plan.joins:
            raise UsageError(f"--continue {plan.continues}: its lineage changed since the plan")
    if plan.reviews is not None:
        lineage = _continued_lineage(
            plan.reviews, state=plan.state, home=request.home, option="--run"
        )
        if lineage != plan.reviewed_lineage:
            raise UsageError(f"--run {plan.reviews}: its lineage changed since the plan")
    findings = _findings(plan)
    if findings is not None:
        plan = replace(plan, prompt=fix_prompt(plan.task or "", findings.text))

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
    workflow = plan.workflow
    if workflow is not None:
        target = {"kind": "workflow", "name": workflow.name, "shape": workflow.shape}
        slot: str = workflow.shape
    else:
        target = {"kind": "provider" if role.implicit else "role", "name": role.name}
        slot = "run"

    with ExitStack() as held_locks:
        try:
            entry = _admit(
                registry,
                held_locks,
                run_dir=plan.run_dir,
                target=target,
                repository=repository,
                write=role.write,
                joins=plan.joins,
                continues=plan.continues,
                # A write run's record (§3.10); a review writes nothing.
                providers=() if plan.panel else role.providers,
                findings_from=plan.findings_from,
            )
        except (LockTimeout, RegistryError) as exc:
            # A run that never started leaves nothing behind (codex review of #207).
            if plan.run_dir is not None:
                shutil.rmtree(plan.run_dir, ignore_errors=True)
            raise UsageError(f"{exc}: nothing ran") from None
        if plan.run_dir is None:
            entry.run_dir.mkdir(parents=True, mode=0o700)
        unconfined = role.write and write_is_unconfined(plan)
        try:
            held_locks.enter_context(
                held(
                    plan.state / "unconfined.lock",
                    rank=Rank.UNCONFINED,
                    exclusive=unconfined,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the unconfined lock",
                )
            )
        except LockTimeout:
            _refused(registry, entry)
            if unconfined:
                raise UsageError(
                    "runs and writes still running after the bound: an unconfined write "
                    "waits for none of them; nothing ran"
                ) from None
            raise UsageError(
                "an unconfined write is running: nothing ran; retry once it has ended"
            ) from None

        try:
            _check_isolation(plan)
            for member in plan.panel:
                # Every role of a review's panel runs a rail (§3.8.0), not only the first.
                _check_isolation(replace(plan, role=member.role, environment=member.environment))
        except UsageError:
            _refused(registry, entry)
            raise

        bundle = _bundle(role, request, identity.work_tree if identity is not None else None)
        try:
            _check_prompt_size(role, plan.prompt, bundle)
        except UsageError:
            _refused(registry, entry)
            raise

        started = time.monotonic()
        run_dir = entry.run_dir
        # §3.10: prompt.md is the task as given; the provider gets its template around it.
        task = plan.task if plan.task is not None else plan.prompt
        (run_dir / PROMPT_FILE).write_text(task, encoding="utf-8")
        report = new_report(
            run_id=entry.run_id,
            target=target,
            repository=repository,
            pid=os.getpid(),
            started_at=_utc_now(),
        )
        write_report(run_dir, report)
        if plan.panel:
            assert identity is not None
            return _execute_review(
                plan,
                registry=registry,
                entry=entry,
                identity=identity,
                start=start,
                report=report,
                started=started,
                say=say,
            )
        step_name = step_dir_name(1, slot, role.name)
        step_dir = run_dir / "steps" / step_name
        if role.write:
            assert identity is not None
            return _execute_write(
                plan,
                bundle,
                unconfined=unconfined,
                registry=registry,
                entry=entry,
                identity=identity,
                start=start,
                report=report,
                slot=slot,
                step_name=step_name,
                started=started,
                say=say,
                findings_head=findings.head if findings is not None else None,
            )
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
                index=1,
                slot="run",
                role=role.name,
                step_dir=f"steps/{step_name}",
                result=final,
                tools=tool_counts(final),
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


def _remove_review_worktree(
    worktree: Path, repository: Path, state: Path, environ: Mapping[str, str]
) -> str | None:
    """``git worktree remove`` of a review's kept worktree; the reason when it cannot."""
    try:
        identity = discover(repository)
    except RepoError as exc:
        return str(exc)
    if identity is None:
        return f"{repository} is no longer a git repository"
    return review_flow.remove_worktree(worktree, identity, operator_environment(environ), state)


def clean(
    run_id: str, *, environ: Mapping[str, str], home: Path, say: Callable[[str], None]
) -> int:
    """``ha clean RUN_ID`` for a run outside any lineage (spec §3.9).

    Resolved through the registry; the run's lifecycle lock taken without
    waiting (an active run is refused), then the unconfined lock shared. A run
    that never started is forgotten; any other has its directory removed and
    ``cleaned_at`` set -- its entry stays. A write run follows the lineage
    rules of :func:`headless_agents.write_flow.clean_write`.
    """
    state = state_dir(environ, home=home)
    registry = Registry(state, runs_root=runs_root(home))
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        raise UsageError(str(exc)) from None
    except Unknown as exc:
        say(f"{exc}: recover it by hand; nothing cleaned")
        return 1
    with ExitStack() as held_locks:
        try:
            held_locks.enter_context(
                held(
                    registry.lifecycle_lock(run_id),
                    rank=Rank.LIFECYCLE,
                    exclusive=True,
                    wait=None,
                    what=f"the lifecycle lock of {run_id}",
                )
            )
        except LockTimeout:
            raise UsageError(f"{run_id} is active: nothing cleaned") from None
        try:
            held_locks.enter_context(
                held(
                    state / "unconfined.lock",
                    rank=Rank.UNCONFINED,
                    exclusive=False,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the unconfined lock",
                )
            )
        except LockTimeout:
            raise UsageError("an unconfined write is running: nothing cleaned") from None
        if entry.lineage is not None:
            try:
                return write_flow.clean_write(
                    run_id=run_id,
                    run_dir=entry.run_dir,
                    owner=entry.lineage,
                    state=state,
                    environ=operator_environment(environ),
                    say=say,
                    forget=lambda: registry.forget(run_id),
                    cleaned=lambda: registry.set_cleaned(run_id, _utc_now()),
                )
            except LockTimeout as exc:
                raise UsageError(f"{exc}: the lineage is in use; nothing cleaned") from None
        started = (entry.run_dir / RUN_JSON).is_file()
        worktree = entry.run_dir / review_flow.WORKTREE
        if worktree.is_dir() and entry.repository is not None:
            # A review whose cleanup failed kept its detached worktree: removed through
            # git, and no git at all under a quarantine (§3.9).
            reason = _remove_review_worktree(worktree, entry.repository, state, environ)
            if reason is not None:
                say(f"{run_id}: its worktree {worktree} is kept ({reason}); nothing cleaned")
                return 1
        if entry.run_dir.is_dir():
            shutil.rmtree(entry.run_dir)
        if not started:
            registry.forget(run_id)
            say(f"{run_id} never started: forgotten")
            return 0
        registry.set_cleaned(run_id, _utc_now())
        say(f"{run_id} cleaned: {entry.run_dir} removed")
        return 0


__all__ = [
    "DEFAULT_CREDENTIALS",
    "Config",
    "clean",
    "declared_roles",
    "describe_roles",
    "describe_workflows",
    "DEFAULT_EXECUTABLES",
    "Outcome",
    "execute",
    "executable_for",
    "load_config",
    "runs_root",
    "Overrides",
    "Plan",
    "REVIEW_DEFAULT_TASK",
    "SlotPlan",
    "Request",
    "UsageError",
    "operator_environment",
    "plan",
    "prompt_is_optional",
]
