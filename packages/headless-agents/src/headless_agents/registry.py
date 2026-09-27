"""The facade: providers by name, a zero-quota probe, per-provider prompt limits, tool counts.

A consumer that dispatches on a provider name through its own ``if/elif``
rebuilds this module, and drifts from it the day a rail is added. It asks
here instead: :func:`get_provider` for the rail, :func:`probe` to know whether
the rail can run on this machine at all, :func:`max_prompt_bytes` for the
argv rails' limit -- never a hard-coded copy of it -- and :func:`tool_counts`
for the tool calls a run made, read from its rail's own event log.

:func:`probe` costs no quota: for a CLI rail it looks for the executable and
asks for its ``--version``; for an HTTP provider it checks that the key
variable is set. Never a model call.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from .capability import scoped_environment
from .keys import KeysError, preset_key
from .protocol import AgentProvider
from .providers.agy import MAX_PROMPT_BYTES as AGY_MAX_PROMPT_BYTES
from .providers.agy import AgyProvider
from .providers.agy import count_tools as agy_tools
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .providers.codex import count_tools as codex_tools
from .providers.openai_compat import GENERIC_NAME, PRESETS, OpenAICompatProvider
from .providers.opencode import MAX_PROMPT_BYTES as OPENCODE_MAX_PROMPT_BYTES
from .providers.opencode import OpenCodeProvider
from .providers.opencode import count_tools as opencode_tools
from .result import RunResult

_FACTORIES: Final[Mapping[str, Callable[[], AgentProvider]]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "agy": AgyProvider,
    "opencode": OpenCodeProvider,
    **{name: partial(OpenAICompatProvider, name) for name in (*PRESETS, GENERIC_NAME)},
}

PROVIDER_NAMES: Final[tuple[str, ...]] = tuple(_FACTORIES)

#: The providers that speak HTTP rather than run a CLI.
HTTP_PROVIDER_NAMES: Final[frozenset[str]] = frozenset((*PRESETS, GENERIC_NAME))

# ``None`` for the rails that read the prompt on stdin and for the HTTP
# providers. agy and opencode MUST take it in argv (agy ignores stdin;
# ``opencode run`` blocks on a piped one), where a single argument is bounded
# by the kernel.
_MAX_PROMPT_BYTES: Final[Mapping[str, int | None]] = {
    "claude": None,
    "codex": None,
    "agy": AGY_MAX_PROMPT_BYTES,
    "opencode": OPENCODE_MAX_PROMPT_BYTES,
    **dict.fromkeys(HTTP_PROVIDER_NAMES),
}

PROBE_TIMEOUT_SECONDS = 10.0


class UnknownProvider(ValueError):
    """A provider name the registry does not know; the message lists the valid ones."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown provider {name!r}; valid names: {', '.join(PROVIDER_NAMES)}")
        self.name = name


@dataclass(frozen=True, kw_only=True)
class Probe:
    """Whether a rail can run here. ``detail`` says where it was found, or why not."""

    available: bool
    detail: str
    version: str | None = None


def _known(name: str) -> str:
    if name not in _FACTORIES:
        raise UnknownProvider(name)
    return name


def get_provider(name: str) -> AgentProvider:
    """A new instance of the rail called ``name``."""
    return _FACTORIES[_known(name)]()


def max_prompt_bytes(name: str) -> int | None:
    """The largest prompt, in UTF-8 bytes, the rail accepts; ``None`` when unbounded."""
    return _MAX_PROMPT_BYTES[_known(name)]


#: The rails whose own event log counts their tool calls (spec §3.11). claude
#: is not here: its counts stay ``None`` until a live test proves its
#: telemetry complete at exit (plan P3).
_TOOL_COUNTERS: Final[Mapping[str, Callable[[Path | None], dict[str, int] | None]]] = {
    "codex": codex_tools,
    "opencode": opencode_tools,
    "agy": agy_tools,
}


def tool_counts(result: RunResult) -> dict[str, int] | None:
    """The tool calls of ``result``'s run, by the rail's own names (spec 0.5.0 §3.11).

    ``{}`` for an HTTP provider -- it has no tools; ``None`` -- not measured --
    for claude (plan P3) and for a rail whose event log cannot be read whole.
    """
    name = _known(result.provider)
    if name in HTTP_PROVIDER_NAMES:
        return {}
    counter = _TOOL_COUNTERS.get(name)
    return None if counter is None else counter(result.events_log)


#: Each CLI rail's OWN documented home/config variable, kept for THAT rail's
#: subprocess (a version probe, or a vendor updater) alone -- never another
#: rail's: claude resolves its own config directory from CLAUDE_CONFIG_DIR
#: (providers.claude.CHILD_ENV_PASSTHROUGH), codex its state directory from
#: CODEX_HOME (providers.codex.CHILD_ENV_PASSTHROUGH, which holds nothing
#: else). agy and opencode read neither: both resolve their home from HOME
#: alone (providers.agy._source_home, providers.opencode's own equivalent),
#: which :func:`probe_environment` forces below regardless of a rail's own
#: passthrough. Deliberately NOT importing each provider's own
#: CHILD_ENV_PASSTHROUGH: claude's and codex's also carry OTEL, MCP and API
#: key variables that a version probe or an updater has no business seeing.
_RAIL_HOME_PASSTHROUGH: Final[Mapping[str, frozenset[str]]] = {
    "claude": frozenset({"CLAUDE_CONFIG_DIR"}),
    "codex": frozenset({"CODEX_HOME"}),
    "agy": frozenset(),
    "opencode": frozenset(),
}


def probe_environment(rail: str, home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a CLI rail's own subprocess -- a version probe, or a vendor
    updater -- should run with: the shared child-environment allowlist
    (:func:`capability.scoped_environment` -- PATH, HOME, locale, proxy and CA
    variables), plus, for the rail alone, its own documented home/config variable
    (see :data:`_RAIL_HOME_PASSTHROUGH`). ``HOME`` is forced to ``home``, so the
    subprocess reads the same identity the caller resolved its executable against.

    A name that is not one of the four CLI rails -- an HTTP provider, say -- gets
    ``environ`` back UNCHANGED: :func:`probe` never spawns a subprocess for it, only
    checks a key variable's presence, and sanitising here would hide the very key
    a caller is checking for.
    """
    passthrough = _RAIL_HOME_PASSTHROUGH.get(rail)
    if passthrough is None:
        return dict(environ)
    child = scoped_environment(environ, passthrough=passthrough)
    child["HOME"] = str(home)
    return child


def _probe_http(name: str, environ: Mapping[str, str]) -> Probe:
    preset = PRESETS.get(name)
    if preset is None:
        # The generic provider's URL and key variable are per run (RunSpec.extra).
        return Probe(available=True, detail="configured per run (base_url, key_env)")
    try:
        found = preset_key(name, environ)
    except KeysError as exc:
        # A declared key file that cannot be used: unavailable, and the probe says why.
        return Probe(available=False, detail=str(exc))
    if found is None:
        return Probe(
            available=False,
            detail=f"{preset.key_env} is not set, and keys.toml declares no file for {name}",
        )
    if found.source == "environment":
        return Probe(available=True, detail=f"{preset.key_env} is set")
    return Probe(available=True, detail=f"{preset.key_env} from {found.source}")


def _first_nonempty_line(text: str) -> str | None:
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """Reap a timed-out probe without leaving a descendant running.

    ``process.kill()`` alone reaches only the direct child: a wrapper that forks
    (a shim, a node launcher) leaves a grandchild sharing its stdout/stderr pipe,
    and that grandchild keeps running -- and the pipe open -- past the direct
    child's death. ``start_new_session=True`` makes the direct child the leader
    of its own process group, so ``killpg`` reaches it and everything it forked
    in one signal.

    The group id is ``process.pid`` itself, never ``getpgid(process.pid)``: a
    group outlives its leader while any member lives, but ``getpgid`` fails
    once the leader is reaped -- and CPython's ``communicate()`` reaps an
    exited launcher when a ``KeyboardInterrupt`` lands, which would spare the
    surviving descendants (independent review of PR #197, reproduced with a
    real SIGINT). Then the direct child is reaped on its own, bounded, and the
    pipes are closed rather than drained: a descendant that escaped the group
    (a second ``setsid``) must not revive the hang we just killed -- that one
    is out of this guarantee's reach.
    """
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()


def probe(
    name: str,
    *,
    executable: str | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> Probe:
    """Can the provider run here?

    A CLI rail: is its executable on ``PATH``, and does it answer ``--version``?
    ``executable`` overrides the rail's default command, as ``RunSpec.executable``
    does for a run. A ``--version`` that does not answer within
    ``timeout_seconds`` makes the rail unavailable: a probe never hangs, and it
    leaves no descendant of the probed executable running behind it (see
    :func:`_kill_process_group`). The version is the first non-empty line of
    stdout, or of stderr when a CLI prints it there instead.

    An HTTP provider: is its key variable set in ``environ`` (default: this
    process's environment)? The detail names the variable, never its value.

    For a CLI rail, ``environ`` -- when given -- is what the ``--version``
    subprocess ITSELF runs with, verbatim: this function never widens or
    narrows it. A caller with an operator environment to hand a rail's own
    subprocess builds it with :func:`probe_environment` first; passing the raw
    process environment through unfiltered is the caller's choice, not a
    default made here silently (default, when ``environ`` is omitted: this
    process's own environment, as before).
    """
    _known(name)
    if name in HTTP_PROVIDER_NAMES:
        return _probe_http(name, os.environ if environ is None else environ)
    # The four CLI rails are invoked by their own name, as the providers do.
    command = executable or name
    path = shutil.which(command)
    if path is None:
        return Probe(available=False, detail=f"{command}: not found or not executable")
    try:
        process = subprocess.Popen(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # A broken CLI gives a reason, never an exception: bytes that are
            # not UTF-8 are replaced instead of raising while decoding.
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=dict(os.environ if environ is None else environ),
        )
    except OSError as exc:
        return Probe(available=False, detail=f"{path} --version: {exc}")
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        return Probe(
            available=False, detail=f"{path} --version: no answer within {timeout_seconds:g} s"
        )
    except BaseException:
        # Its own session keeps the probed CLI out of the terminal's foreground
        # group: a Ctrl-C reaches only us, so the group is ours to kill. A
        # cleanup that fails must never mask the interrupt the caller caused.
        with suppress(Exception):
            _kill_process_group(process)
        raise
    if process.returncode != 0:
        return Probe(available=False, detail=f"{path} --version exited {process.returncode}")
    version = _first_nonempty_line(stdout) or _first_nonempty_line(stderr)
    return Probe(available=True, detail=path, version=version)
