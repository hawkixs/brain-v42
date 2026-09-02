"""Provider-agnostic capability boundary shared by the Dream agent runners.

Every agent rail — Codex today, Claude alongside it — has to answer the same
four questions before it may speak to the Brain MCP server: is enforcement on,
is the URL loopback, which ``(project, phase)`` bearer is active, and which
ambient variables may cross into the child process.

Those answers used to live inside ``codex_runner`` as private helpers.  They
are extracted here unchanged rather than copied, because a second copy is how
two rails drift into disagreeing about the same firewall — and the rail that
drifts quietly is the one that stops being scoped without failing.

The child-environment allowlist is deliberately a *base* set plus a per-rail
extension: Codex and Claude do not need the same variables, and widening the
shared set to satisfy one rail would hand the other variables it never asked
for.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    DreamCapabilityRegistry,
    parse_dream_capability_registry,
)
from brain_v42.models.project_key import canonicalize_project_key

DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"
MCP_URL_ENV = "BRAIN_DREAM_MCP_URL"
MCP_TOKEN_ENV = "MCP_HTTP_TOKEN"
DREAM_TOKENS_ENV = "MCP_HTTP_DREAM_TOKENS"
CAPABILITY_ENFORCEMENT_ENV = "BRAIN_DREAM_CAPABILITY_ENFORCEMENT"
CAPABILITY_CONFIGURATION_ERROR = "Dream capability configuration is invalid"
LOOPBACK_MCP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_NO_PROXY_ENTRIES = ("127.0.0.1", "localhost", "::1")
TERMINATION_GRACE_SECONDS = 5.0

# The exit code that lets `dream.sh` replay the phase on the NEXT provider. It
# does not say "I failed" — 1 already says that — but "I failed AND I can
# PROVE that no Brain tool call succeeded", hence that no WET mutation was
# committed.
#
# It is the only thing that makes a switchover safe. dream.sh forbade any
# fallback for that exact reason for a long time, and the ban is not lifted but
# refined: "never switch over" becomes "switch over on proof only". Widening the
# condition to "rc != 0" would make the night able to replay a phase that had
# already written, silently, doubling its writes.
PROVIDER_FALLBACK_EXIT_CODE = 3

# The conventional "deadline exceeded" code, the one from `timeout(1)`.
#
# The runners now own their own deadline and return it on TimeoutExpired.
# But a CHILD that exits with 124 itself must keep being read as a timeout:
# that is what `timeout ${n}m claude ...` did, and the whole downstream
# chain — log, metrics, the night's retry budget — is written around that
# convention.
#
# The ambiguity is accepted and bounded: a CLI choosing 124 for a reason of its
# own would be mislabelled. That is the right trade, because erring in this
# direction REFUSES the switchover (a timeout proves nothing), where the reverse
# would authorise it on a phase that may have written.
TIMEOUT_EXIT_CODE = 124

# Variables every rail needs to run at all: locale, TLS trust, proxy policy and
# the paths a CLI resolves against. Rail-specific additions go through the
# ``extra_allowlist`` argument, never in here.
BASE_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "COLORTERM",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


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


def validate_loopback_mcp_url(environ: Mapping[str, str]) -> None:
    mcp_url = environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    try:
        parsed = urlsplit(mcp_url)
        _ = parsed.port
    except (TypeError, ValueError):
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR) from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOOPBACK_MCP_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise DreamCapabilityConfigurationError(CAPABILITY_CONFIGURATION_ERROR)


def merged_no_proxy(environ: Mapping[str, str]) -> str:
    entries: list[str] = []
    for variable_name in ("NO_PROXY", "no_proxy"):
        for raw_entry in environ.get(variable_name, "").split(","):
            entry = raw_entry.strip()
            if entry and entry not in entries:
                entries.append(entry)
    for entry in LOOPBACK_NO_PROXY_ENTRIES:
        if entry not in entries:
            entries.append(entry)
    return ",".join(entries)


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
    path usable on a host that has no capability registry at all.
    """
    if not capability_enforcement_enabled(environ):
        return None
    validate_loopback_mcp_url(environ)
    active_token = active_capability_token(
        project_key=project_key,
        phase=phase,
        environ=environ,
    )
    allowlist = BASE_CHILD_ENV_ALLOWLIST | frozenset(extra_allowlist)
    child_environment = {name: value for name, value in environ.items() if name in allowlist}
    no_proxy = merged_no_proxy(environ)
    child_environment["NO_PROXY"] = no_proxy
    child_environment["no_proxy"] = no_proxy
    # The scoped bearer replaces the admin token under the SAME name: the MCP
    # client configurations reference it by environment variable, so swapping
    # the value is what narrows the phase without writing a secret anywhere.
    child_environment[MCP_TOKEN_ENV] = active_token
    return child_environment


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


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the agent and every child it spawned, escalating after 5s."""
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return

    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()  # Reap the leader; children may still keep the group alive.
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
