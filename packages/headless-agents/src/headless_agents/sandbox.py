"""Filesystem sandbox for headless agent runs: ephemeral HOMEs and credentials.

Process-level primitives (the child-environment allowlist, ``NO_PROXY``
merging, process-group termination) live in :mod:`headless_agents.capability`.
This module builds the throwaway ``HOME`` a CLI that takes no per-invocation
configuration flag (``agy``) needs, so the bearer, the MCP config and the
tool-use guard can all be scoped to one run without touching the real HOME or
a global config file two concurrent runs would otherwise fight over.

What goes into the HOME is read off a :class:`~headless_agents.profile.CapabilityProfile`:

- ``profile.mcp`` -> ``.gemini/config/mcp_config.json`` (one server with a
  LITERAL bearer -- agy's ``Authorization`` is a literal, its documentation
  describes no ``${VAR}`` interpolation), or ``{"mcpServers": {}}`` when
  ``None``;
- ``profile.guard`` -> ``.gemini/config/hooks.json`` wiring the caller's
  ``PreToolUse`` script, or no file at all when ``None``;
- ``profile.credentials`` -> symlinks (default) or ``0600`` copies of the
  caller-declared files under the real HOME.

Without a workspace, the guard is never bundled here: it is a versioned file
that lives with its own tests in the caller's tree. WITH a workspace, the
package-owned guard (:mod:`headless_agents.guards.agy_workspace`) is copied
into the HOME with its ``workspace-guard.json`` next to it; the two guards do
not compose, and a profile carrying both is refused. Credentials are never
copied unless the caller says ``mode="copy"``: duplicating a human's OAuth
tokens makes copies to revoke one by one, so the default is a symlink.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from .guards.agy_workspace import GUARD_CONFIG_NAME
from .profile import CapabilityProfile, Credentials, McpServer, ToolGuard, Workspace

# Where a workspace run's copy of the package guard lives, and the name its
# hook is filed under in hooks.json.
WORKSPACE_GUARD_NAME = "workspace_guard.py"
WORKSPACE_HOOK_NAME = "workspace-guard"

# Used when the parent process has no PATH at all. Deliberately poor: enough to
# find a system-wide CLI, nothing more.
FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"


def ephemeral_root(environ: Mapping[str, str]) -> Path | None:
    """Root of the ephemeral HOMEs -- a tmpfs by preference.

    A bearer may be written there: it must not land on persistent disk.
    ``None`` means "no tmpfs available", and lets the caller fall back on
    ``tempfile`` rather than inventing a path.
    """
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir)
    return None


def materialize_credentials(*, home: Path, real_home: Path, credentials: Credentials) -> None:
    """Expose the declared credential files inside ``home``.

    Absent sources are skipped, existing targets are left alone. In ``copy``
    mode the source is resolved and must still sit under ``real_home``: the
    relative-path guard in :class:`Credentials` cannot see a symlink that
    points outside, and a copy would carry the bytes it points at.
    """
    root = real_home.resolve()
    for relative in credentials.paths:
        source = real_home / relative
        target = home / relative
        if not source.exists() or target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if credentials.mode == "symlink":
            target.symlink_to(source)
            continue
        resolved = source.resolve()
        if root not in resolved.parents:
            raise ValueError(f"credential resolves outside the real HOME: {relative!r}")
        shutil.copyfile(resolved, target)
        target.chmod(0o600)


def _mcp_config(mcp: McpServer | None) -> dict[str, object]:
    if mcp is None:
        return {"mcpServers": {}}
    if mcp.bearer is None:
        raise ValueError(
            "an ephemeral HOME writes the bearer literally: McpServer.bearer is required"
        )
    return {
        "mcpServers": {
            mcp.name: {
                "serverUrl": mcp.url,
                "headers": {
                    "Authorization": f"Bearer {mcp.bearer.get_secret_value()}",
                    **dict(mcp.headers),
                },
                "trust": True,
            }
        }
    }


def build_ephemeral_home(
    *,
    root: Path,
    name: str,
    profile: CapabilityProfile,
    real_home: Path,
    workspace: Workspace | None = None,
    guard_python: str = sys.executable,
) -> Path:
    """Compose one run's HOME under ``root/name``: server, guard, settings, credentials.

    ``workspace`` swaps the caller's guard for the package's own, run by
    ``guard_python`` -- the interpreter this runtime lives in: the guard needs
    the standard library only, and agy's rebuilt environment names no other.
    """
    if workspace is not None and profile.guard is not None:
        raise ValueError(
            "a workspace run is confined by the package's own tool_guard:"
            " profile.guard must be None, the two do not compose"
        )
    home = root / name
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps(_mcp_config(profile.mcp)), encoding="utf-8")
    config_path.chmod(0o600)

    guard = profile.guard
    if workspace is not None:
        guard = ToolGuard(
            path=_install_workspace_guard(config_dir, workspace, guard_python),
            hook_name=WORKSPACE_HOOK_NAME,
        )
    if guard is not None:
        hooks = {
            guard.hook_name: {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(guard.path),
                                "timeout": guard.timeout_seconds,
                            }
                        ],
                    }
                ]
            }
        }
        (config_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    # The trusted workspace must be the ephemeral HOME itself: without it, agy
    # refuses to load its customisations. A workspace run also trusts the
    # directory it works in, which is its cwd.
    trusted = [str(home)] if workspace is None else [str(home), str(workspace.path)]
    (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"enableTelemetry": False, "trustedWorkspaces": trusted}),
        encoding="utf-8",
    )

    materialize_credentials(home=home, real_home=real_home, credentials=profile.credentials)
    return home


def _install_workspace_guard(config_dir: Path, workspace: Workspace, guard_python: str) -> Path:
    """Copy the package guard into the HOME, on ``guard_python``, with its config.

    Read through ``importlib.resources`` so it is the file the installed wheel
    carries, not a source-tree path. The shebang is written, not inherited:
    agy runs the hook as a bare command, and the module ships with none.
    """
    source = importlib.resources.files("headless_agents.guards").joinpath("agy_workspace.py")
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    path = config_dir / WORKSPACE_GUARD_NAME
    path.write_text(f"#!{guard_python}\n" + "".join(lines), encoding="utf-8")
    path.chmod(0o700)
    guard_config = config_dir / GUARD_CONFIG_NAME
    guard_config.write_text(
        json.dumps(
            {"root": str(workspace.path), "write": workspace.write, "shell": workspace.shell}
        ),
        encoding="utf-8",
    )
    guard_config.chmod(0o600)
    return path


def build_toolless_home(
    *,
    root: Path,
    name: str,
    real_home: Path,
    credentials: Credentials,
) -> Path:
    """Compose a HOME that declares no server, wires no guard and trusts nothing.

    The variant for a run that gives the agent nothing to call: only the
    credentials it needs to authenticate.
    """
    home = root / name
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)

    materialize_credentials(home=home, real_home=real_home, credentials=credentials)
    return home


def sandbox_environment(home: Path, *, environ: Mapping[str, str]) -> dict[str, str]:
    """A child environment REBUILT from scratch. Nothing is inherited but ``PATH``.

    ``HOME`` is the sandbox, never the operator's: that is the variable that
    matters, because a CLI reads its user-level instructions, settings and MCP
    server list under it. ``TMPDIR`` is brought inside so whatever the CLI
    writes disappears with the sandbox. ``PATH`` is the one concession, so a
    CLI installed under a user path stays resolvable. The locale is UTF-8,
    otherwise a POSIX locale mangles the prompt's accents.

    Limit, to read before feeling protected: ``cwd`` constrains relative
    paths only and a clean environment constrains none. The child keeps full
    filesystem access by absolute path. This removes the leak by
    CONFIGURATION; refusing deliberate reads is the CLI's own flags' job.
    """
    return {
        "HOME": str(home),
        "TMPDIR": str(home),
        "PATH": environ.get("PATH") or FALLBACK_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
