"""Filesystem sandbox for headless agent runs: ephemeral HOMEs and credentials.

Process-level primitives (the child-environment allowlist, ``NO_PROXY``
merging, process-group termination) live in :mod:`brain_v42.agents.capability`
-- a near-verbatim port of ``scripts/dream/_agent_capability.py``. This module
adds what that file did not have: constructing a throwaway ``HOME`` directory
for a CLI that takes no per-invocation configuration flag (``agy``), so the
bearer, the MCP config and the tool-use guard can all be scoped to one phase
without touching the real HOME or a global config file two concurrent phases
would otherwise fight over.

Two variants ship, moved unchanged from their two former homes:

- :func:`build_ephemeral_home` -- the nightly Dream agy rail
  (``scripts/dream/agy_runner.py``): a scoped Brain MCP server plus a
  ``PreToolUse`` guard, wired into the ephemeral HOME's
  ``.gemini/config/{mcp_config.json,hooks.json}``.
- :func:`build_toolless_home` -- the extract rescue link
  (``src/brain_v42/scripts/agy_completion.py``): no MCP servers at all, no
  guard, because extract gives agy nothing to call.

The guard is intentionally NOT bundled into the package: it is a versioned
file that lives with its own tests (``scripts/dream/agy_tool_guard.sh`` and
``tests/unit/test_dream_agy_guard.py``), and ``src/brain_v42/`` must not depend
on anything under ``scripts/``. ``build_ephemeral_home`` therefore takes
``guard_path`` as an explicit parameter -- the caller (today,
``scripts/dream/agy_runner.py``) supplies the path to its own versioned guard.
A future in-package consumer that needs a guard would ship and pass its own.

Credential paths are also caller-supplied and never copied, only symlinked:
duplicating a human's OAuth tokens would make copies to revoke one by one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from brain_v42.mcp.dream_capabilities import dream_phase_tool_allowlist

from .capability import (
    DEFAULT_MCP_URL,
    MCP_URL_ENV,
    active_capability_token,
    validate_loopback_mcp_url,
)

# Credential files read from the real HOME by the Dream agy rail. SYMLINKED,
# never copied.
DREAM_AGY_CREDENTIAL_PATHS = (
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
    ".gemini/gemini-credentials.json",
    ".gemini/antigravity-cli/antigravity-oauth-token",
)

# Credential files read from the real HOME by the tool-less extract rail. A
# narrower set than the Dream rail's: extract never uses Google account
# switching, so ``google_accounts.json`` and ``gemini-credentials.json`` were
# never part of its ported behaviour.
EXTRACT_AGY_CREDENTIAL_PATHS = (
    ".gemini/oauth_creds.json",
    ".gemini/antigravity-cli/antigravity-oauth-token",
)


def ephemeral_root(environ: Mapping[str, str]) -> Path | None:
    """Root of the ephemeral HOMEs -- a tmpfs by preference.

    The bearer is written there: it must not land on persistent disk. ``None``
    means "no tmpfs available", and lets the caller fall back on ``tempfile``
    rather than inventing a path.
    """
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir)
    return None


def _symlink_credentials(*, home: Path, real_home: Path, credential_paths: Iterable[str]) -> None:
    for relative in credential_paths:
        source = real_home / relative
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            target.symlink_to(source)


def build_ephemeral_home(
    *,
    root: Path,
    phase: str,
    project_key: str,
    environ: Mapping[str, str],
    real_home: Path,
    guard_path: Path,
    mcp_url: str | None = None,
) -> Path:
    """Compose a phase HOME: scoped bearer, wired guard, nothing else.

    Moved unchanged from ``scripts/dream/agy_runner.py::build_ephemeral_home``,
    save for ``guard_path`` becoming an explicit parameter instead of a
    module-level constant computed from ``__file__`` -- see the module
    docstring for why.
    """
    dream_phase_tool_allowlist(phase)
    validate_loopback_mcp_url(environ)
    token = active_capability_token(project_key=project_key, phase=phase, environ=environ)

    home = root / f"agy-{project_key.replace(':', '-')}-{phase}"
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    # The real HOME's mcp_config also declares red-writer, on a public URL.
    # We do not copy it: we write one that knows brain-v42 and nothing else.
    server_url = mcp_url or environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    mcp_config = {
        "mcpServers": {
            "brain-v42": {
                "serverUrl": server_url,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "X-Brain-Agent": f"dream-agy-{phase}",
                    "X-Brain-Tool-Profile": "native",
                },
                "trust": True,
            }
        }
    }
    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
    config_path.chmod(0o600)

    # The guard comes from the REPOSITORY. A copy dropped next to the secret
    # would be editable without review and would drift from its tests.
    hooks = {
        "dream-phase-guard": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": str(guard_path), "timeout": 10}],
                }
            ]
        }
    }
    (config_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    # The trusted workspace must be the ephemeral HOME itself: without it, agy
    # refuses to load its customisations.
    (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"enableTelemetry": False, "trustedWorkspaces": [str(home)]}),
        encoding="utf-8",
    )

    _symlink_credentials(
        home=home, real_home=real_home, credential_paths=DREAM_AGY_CREDENTIAL_PATHS
    )

    return home


def build_toolless_home(root: Path, *, real_home: Path | None = None) -> Path:
    """Compose a tool-less ephemeral HOME: no MCP servers, symlinked credentials.

    Moved unchanged from
    ``src/brain_v42/scripts/agy_completion.py::build_toolless_home``.
    ``real_home`` defaults to the process's own ``HOME`` -- parametrised so
    tests never touch the real one.
    """
    home = root / "agy-extract-home"
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)

    source_home = (
        real_home if real_home is not None else Path(os.environ.get("HOME", str(Path.home())))
    )
    _symlink_credentials(
        home=home, real_home=source_home, credential_paths=EXTRACT_AGY_CREDENTIAL_PATHS
    )

    return home
