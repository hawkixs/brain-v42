"""Process-level primitives every rail shares: exit codes, the child
environment, loopback validation, process-group termination.

Ported from ``brain_v42.agents.capability`` (itself a port of
``scripts/dream/_agent_capability.py``) with the Dream policy left behind:
this module no longer knows what a capability registry is, which
``(project, phase)`` bearer is active, or whether "enforcement" is on. The
caller resolves those and hands the result in -- a bearer value through
``overrides``, a rail's extra variables through ``passthrough``.

The child-environment allowlist is deliberately a *base* set plus per-rail
extensions: Codex and Claude do not need the same variables, and widening the
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

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_NO_PROXY_ENTRIES = ("127.0.0.1", "localhost", "::1")
TERMINATION_GRACE_SECONDS = 5.0

# The exit code that lets a chain replay the run on the NEXT provider. It does
# not say "I failed" -- 1 already says that -- but "I failed AND I can PROVE
# that no tool call succeeded", hence that no mutation was committed.
#
# It is the only thing that makes a switchover safe. Widening the condition to
# "rc != 0" would let a chain replay a run that had already written, silently,
# doubling its writes.
PROVIDER_FALLBACK_EXIT_CODE = 3

# The conventional "deadline exceeded" code, the one from ``timeout(1)``. The
# runners own their own deadline and return it on ``TimeoutExpired``; a CHILD
# that exits with 124 itself is read as a timeout too. The ambiguity is
# accepted and bounded: erring in this direction REFUSES the switchover (a
# timeout proves nothing), where the reverse would authorise it on a run that
# may have written.
TIMEOUT_EXIT_CODE = 124

# Variables every rail needs to run at all: locale, TLS trust, proxy policy and
# the paths a CLI resolves against. Rail-specific additions go through
# ``passthrough``, never in here.
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


def validate_loopback_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is a plain http(s) loopback URL."""
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except (TypeError, ValueError):
        raise ValueError(f"not a loopback URL: {url!r}") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise ValueError(f"not a loopback URL: {url!r}")


def merged_no_proxy(environ: Mapping[str, str]) -> str:
    """``NO_PROXY``/``no_proxy`` merged, with the loopback entries appended once."""
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


def scoped_environment(
    environ: Mapping[str, str],
    *,
    passthrough: Iterable[str] = (),
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The child environment: the allowlisted subset of ``environ``, loopback
    added to ``NO_PROXY``, then ``overrides`` applied last.

    A caller narrowing a bearer passes it in ``overrides`` under the name the
    rail's configuration references: swapping the value is what scopes the run
    without writing a secret anywhere.
    """
    allowlist = BASE_CHILD_ENV_ALLOWLIST | frozenset(passthrough)
    child = {name: value for name, value in environ.items() if name in allowlist}
    no_proxy = merged_no_proxy(environ)
    child["NO_PROXY"] = no_proxy
    child["no_proxy"] = no_proxy
    if overrides:
        child.update(overrides)
    return child


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the agent and every child it spawned, escalating after a grace period.

    Requires the process to have been started with ``start_new_session=True``
    so that its pid is also its process-group id.
    """
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
