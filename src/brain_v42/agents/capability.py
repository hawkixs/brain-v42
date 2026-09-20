"""The Dream's capability POLICY over the shared runtime's mechanics.

Every agent rail -- Codex, agy, Claude -- has to answer the same four
questions before it may speak to the Brain MCP server: is enforcement on, is
the URL loopback, which ``(project, phase)`` bearer is active, and which
ambient variables may cross into the child process.

Since Brain ticket b2a2d1a5 the MECHANICS live in
:mod:`headless_agents.capability` (the base allowlist, ``NO_PROXY`` merging,
loopback validation, exit codes, process-group termination) and this module
keeps only what is Dream POLICY: the ``BRAIN_DREAM_*`` variables, the
capability registry, the phase allowlists, and the translation of all that
into the :class:`~headless_agents.profile.McpServer` the runtime consumes.
The runtime never learns what a Dream phase is; the Dream never re-implements
what a child environment is.

The names re-exported below are the ones ``scripts/dream/_agent_capability.py``
and the pre-existing tests import from here; they resolve to the runtime's
objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import SecretStr

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    DreamCapabilityRegistry,
    dream_phase_tool_allowlist,
    parse_dream_capability_registry,
)
from brain_v42.models.project_key import canonicalize_project_key
from headless_agents.capability import (
    BASE_CHILD_ENV_ALLOWLIST as BASE_CHILD_ENV_ALLOWLIST,
)
from headless_agents.capability import (
    LOOPBACK_HOSTS,
    scoped_environment,
    validate_loopback_url,
)
from headless_agents.capability import (
    LOOPBACK_NO_PROXY_ENTRIES as LOOPBACK_NO_PROXY_ENTRIES,
)
from headless_agents.capability import (
    PROVIDER_FALLBACK_EXIT_CODE as PROVIDER_FALLBACK_EXIT_CODE,
)
from headless_agents.capability import (
    TERMINATION_GRACE_SECONDS as TERMINATION_GRACE_SECONDS,
)
from headless_agents.capability import (
    TIMEOUT_EXIT_CODE as TIMEOUT_EXIT_CODE,
)
from headless_agents.capability import (
    TIMEOUT_REPLAYABLE_EXIT_CODE as TIMEOUT_REPLAYABLE_EXIT_CODE,
)
from headless_agents.capability import (
    merged_no_proxy as merged_no_proxy,
)
from headless_agents.capability import (
    terminate_process_group as terminate_process_group,
)
from headless_agents.profile import McpServer

LOOPBACK_MCP_HOSTS = LOOPBACK_HOSTS
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"
MCP_URL_ENV = "BRAIN_DREAM_MCP_URL"
MCP_TOKEN_ENV = "MCP_HTTP_TOKEN"
DREAM_TOKENS_ENV = "MCP_HTTP_DREAM_TOKENS"
CAPABILITY_ENFORCEMENT_ENV = "BRAIN_DREAM_CAPABILITY_ENFORCEMENT"
CAPABILITY_CONFIGURATION_ERROR = "Dream capability configuration is invalid"

# The name under which every Dream rail declares the Brain server to its CLI:
# `mcp_servers.brain-v42.*` for codex, `mcpServers["brain-v42"]` for claude
# and agy, `mcp__brain-v42__<tool>` in claude's allowlist.
BRAIN_MCP_SERVER_NAME = "brain-v42"


def capability_enforcement_enabled(environ: Mapping[str, str]) -> bool:
    value = environ.get(CAPABILITY_ENFORCEMENT_ENV, "false")
    if value == "false":
        return False
    if value == "true":
        return True
    raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR)


def capability_registry(environ: Mapping[str, str]) -> DreamCapabilityRegistry:
    raw_registry = environ.get(DREAM_TOKENS_ENV)
    admin_token = environ.get(MCP_TOKEN_ENV)
    if raw_registry is None or admin_token is None:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR)
    return parse_dream_capability_registry(raw_registry, admin_token=admin_token)


def canonical_capability_project_key(project_key: str | None) -> str:
    if project_key is None:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR)
    try:
        canonical_project_key = canonicalize_project_key(project_key)
    except ValueError:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR) from None
    if canonical_project_key is None:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR)
    return str(canonical_project_key)


def mcp_url(environ: Mapping[str, str]) -> str:
    return environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)


def validate_loopback_mcp_url(environ: Mapping[str, str]) -> None:
    try:
        validate_loopback_url(mcp_url(environ))
    except ValueError:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR) from None


def active_capability_token(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
) -> str:
    canonical_project_key = canonical_capability_project_key(project_key)
    registry = capability_registry(environ)
    try:
        return str(registry.active_token_for(canonical_project_key, phase).get_secret_value())
    except KeyError:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR) from None


def build_child_environment(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
    extra_allowlist: Iterable[str] = (),
) -> dict[str, str] | None:
    """Return the scoped child environment, or ``None`` when enforcement is off.

    ``None`` means "inherit the ambient environment", which keeps the rollback
    path usable on a host that has no capability registry at all. The scoped
    bearer replaces the admin token under the SAME name: the MCP client
    configurations reference it by environment variable, so swapping the
    value is what narrows the phase without writing a secret anywhere.
    """
    if not capability_enforcement_enabled(environ):
        return None
    validate_loopback_mcp_url(environ)
    active_token = active_capability_token(
        project_key=project_key,
        phase=phase,
        environ=environ,
    )
    return scoped_environment(
        environ,
        passthrough=extra_allowlist,
        overrides={MCP_TOKEN_ENV: active_token},
    )


def brain_mcp_server(
    *,
    agent: str,
    phase: str,
    url: str,
    bearer: str | None = None,
    require_loopback: bool = False,
) -> McpServer:
    """The Brain server as one Dream ``(rail, phase)`` declares it to its CLI.

    The tool allowlist is the phase's; ``agent`` is the ``X-Brain-Agent``
    value, spelled by each rail as its own ``f"dream-<rail>-{phase}"`` literal
    so ``test_actor_classification_covers_every_dream_rail.py`` keeps reading
    every emitted header off the source; the bearer travels under
    ``MCP_HTTP_TOKEN`` for the rails that read it from their environment
    (codex, claude). ``bearer`` carries the ``(project, phase)`` token VALUE
    for the one rail that must write it literally (agy). ``require_loopback``
    is off by default because the argv builders are pure and the URL is
    validated by :func:`build_child_environment` under enforcement; the
    historical rollback path never validated it.
    """
    return McpServer(
        name=BRAIN_MCP_SERVER_NAME,
        url=url,
        bearer=SecretStr(bearer) if bearer is not None else None,
        bearer_env_var=MCP_TOKEN_ENV,
        headers={
            "X-Brain-Agent": agent,
            "X-Brain-Tool-Profile": "native",
        },
        tools=dream_phase_tool_allowlist(phase),
        require_loopback=require_loopback,
    )


def preflight_capabilities(project_key: str, environ: Mapping[str, str]) -> None:
    """Fail before any phase starts when the project lacks a complete matrix."""
    if not capability_enforcement_enabled(environ):
        return
    validate_loopback_mcp_url(environ)
    canonical_project_key = canonical_capability_project_key(project_key)
    registry = capability_registry(environ)
    try:
        for phase in DREAM_PHASE_TOOL_ALLOWLISTS:
            registry.active_token_for(canonical_project_key, phase)
    except KeyError:
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR) from None
