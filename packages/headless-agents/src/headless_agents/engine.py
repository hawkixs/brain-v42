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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .capability import scoped_environment
from .cli_models import ModelsError, models_for
from .config_paths import ConfigPathError, config_file, state_dir
from .context import ContextLevel, resolve_context, role_instructions
from .mcp_profiles import PROFILES_FILE_NAME, McpProfileError, load_profiles, mcp_server
from .profile import McpServer, mcp_no_proxy_hosts
from .providers.claude import MAX_APPEND_SYSTEM_PROMPT_BYTES
from .registry import max_prompt_bytes
from .roles import Role, RolesError, capability_rule, load_roles, resolve_role
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
        shell=overrides.shell if overrides.shell is not None else role.shell,
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


def _check_prompt_size(role: Role, prompt: str, request: Request) -> None:
    """Refuse a prompt a link cannot carry -- every link, not only the first (§3.4).

    The preamble counted here is a lower bound: the role's instructions and the
    operator's user-level files. ``execute`` checks again with the real bundle,
    once the repository is known.
    """
    bundle = resolve_context(
        level=role.context,
        repository_root=None,
        user_files=(request.home / ".claude" / "CLAUDE.md",),
    )
    if role.instructions:
        bundle = bundle.with_role(role_instructions(role.name, role.instructions))
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
    _check_prompt_size(role, prompt, request)

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


__all__ = [
    "WRITE_NOT_AVAILABLE",
    "Overrides",
    "Plan",
    "Request",
    "UsageError",
    "operator_environment",
    "plan",
]
