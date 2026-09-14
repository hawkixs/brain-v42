"""The Dream's two ephemeral HOMEs, composed by the shared runtime.

Since Brain ticket b2a2d1a5 the directory layout, the credential handling
and the guard wiring live in :mod:`headless_agents.sandbox`; this module only
says WHAT the Dream puts in a HOME -- the scoped Brain server of one
``(project, phase)``, the versioned ``PreToolUse`` guard, the Google
credential files -- and keeps the two signatures its callers and tests use:

- :func:`build_ephemeral_home` -- the nightly Dream agy rail
  (``brain_v42.agents.providers.agy``): a scoped Brain MCP server plus a
  ``PreToolUse`` guard, wired into the ephemeral HOME's
  ``.gemini/config/{mcp_config.json,hooks.json}``.
- :func:`build_toolless_home` -- the extract rescue link
  (``src/brain_v42/scripts/agy_completion.py``): no MCP servers at all, no
  guard, because extract gives agy nothing to call.

The guard is intentionally NOT bundled into either package: it is a versioned
file that lives with its own tests (``scripts/dream/agy_tool_guard.sh`` and
``tests/unit/test_dream_agy_guard.py``), and ``src/brain_v42/`` must not
depend on anything under ``scripts/``. ``build_ephemeral_home`` therefore
takes ``guard_path`` as an explicit parameter.

Credential paths are symlinked, never copied: duplicating a human's OAuth
tokens would make copies to revoke one by one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from brain_v42.mcp.dream_capabilities import dream_phase_tool_allowlist
from headless_agents.profile import CapabilityProfile, Credentials, ToolGuard
from headless_agents.sandbox import build_ephemeral_home as _build_ephemeral_home
from headless_agents.sandbox import build_toolless_home as _build_toolless_home
from headless_agents.sandbox import ephemeral_root as ephemeral_root

from .capability import (
    DEFAULT_MCP_URL,
    MCP_URL_ENV,
    active_capability_token,
    brain_mcp_server,
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

# The hook key agy reads in ``hooks.json``. Pinned by the golden fixtures.
DREAM_GUARD_HOOK_NAME = "dream-phase-guard"


def dream_agy_profile(
    *,
    phase: str,
    project_key: str,
    environ: Mapping[str, str],
    guard_path: Path,
    mcp_url: str | None = None,
) -> CapabilityProfile:
    """The capability profile of one Dream agy ``(project, phase)`` run.

    The bearer is resolved here, as a VALUE: agy writes it literally into its
    ``mcp_config.json`` (see ``brain_v42.agents.providers.agy``).
    """
    dream_phase_tool_allowlist(phase)
    validate_loopback_mcp_url(environ)
    token = active_capability_token(project_key=project_key, phase=phase, environ=environ)
    # The real HOME's mcp_config also declares other servers, on public URLs.
    # We do not copy it: we write one that knows brain-v42 and nothing else.
    server_url = mcp_url or environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    return CapabilityProfile(
        mcp=brain_mcp_server(agent=f"dream-agy-{phase}", phase=phase, url=server_url, bearer=token),
        # The guard comes from the REPOSITORY. A copy dropped next to the secret
        # would be editable without review and would drift from its tests.
        guard=ToolGuard(path=guard_path, hook_name=DREAM_GUARD_HOOK_NAME, timeout_seconds=10),
        credentials=Credentials(paths=DREAM_AGY_CREDENTIAL_PATHS),
    )


def dream_agy_home_name(project_key: str, phase: str) -> str:
    return f"agy-{project_key.replace(':', '-')}-{phase}"


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
    """Compose a phase HOME: scoped bearer, wired guard, nothing else."""
    profile = dream_agy_profile(
        phase=phase,
        project_key=project_key,
        environ=environ,
        guard_path=guard_path,
        mcp_url=mcp_url,
    )
    return _build_ephemeral_home(
        root=root,
        name=dream_agy_home_name(project_key, phase),
        profile=profile,
        real_home=real_home,
    )


def build_toolless_home(root: Path, *, real_home: Path | None = None) -> Path:
    """Compose a tool-less ephemeral HOME: no MCP servers, symlinked credentials.

    ``real_home`` defaults to the process's own ``HOME`` -- parametrised so
    tests never touch the real one.
    """
    source_home = (
        real_home if real_home is not None else Path(os.environ.get("HOME", str(Path.home())))
    )
    return _build_toolless_home(
        root=root,
        name="agy-extract-home",
        real_home=source_home,
        credentials=Credentials(paths=EXTRACT_AGY_CREDENTIAL_PATHS),
    )
