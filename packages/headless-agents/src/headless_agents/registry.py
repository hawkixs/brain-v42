"""The facade: providers by name, a zero-quota probe, and per-provider prompt limits.

A consumer that dispatches on a provider name through its own ``if/elif``
rebuilds this module, and drifts from it the day a rail is added. It asks
here instead: :func:`get_provider` for the rail, :func:`probe` to know whether
the rail can run on this machine at all, :func:`max_prompt_bytes` for the
argv rails' limit -- never a hard-coded copy of it.

:func:`probe` costs no quota: it looks for the executable and asks for its
``--version``, never for a model call.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .protocol import AgentProvider
from .providers.agy import MAX_PROMPT_BYTES as AGY_MAX_PROMPT_BYTES
from .providers.agy import AgyProvider
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .providers.opencode import MAX_PROMPT_BYTES as OPENCODE_MAX_PROMPT_BYTES
from .providers.opencode import OpenCodeProvider

_FACTORIES: Final[Mapping[str, Callable[[], AgentProvider]]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "agy": AgyProvider,
    "opencode": OpenCodeProvider,
}

PROVIDER_NAMES: Final[tuple[str, ...]] = tuple(_FACTORIES)

# ``None`` for the rails that read the prompt on stdin. agy and opencode MUST
# take it in argv (agy ignores stdin; ``opencode run`` blocks on a piped one),
# where a single argument is bounded by the kernel.
_MAX_PROMPT_BYTES: Final[Mapping[str, int | None]] = {
    "claude": None,
    "codex": None,
    "agy": AGY_MAX_PROMPT_BYTES,
    "opencode": OPENCODE_MAX_PROMPT_BYTES,
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


def probe(
    name: str,
    *,
    executable: str | None = None,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> Probe:
    """Is the rail's executable on ``PATH``, and does it answer ``--version``?

    ``executable`` overrides the rail's default command, as ``RunSpec.executable``
    does for a run. A ``--version`` that does not answer within
    ``timeout_seconds`` makes the rail unavailable: a probe never hangs.
    """
    _known(name)
    # The four CLI rails are invoked by their own name, as the providers do.
    command = executable or name
    path = shutil.which(command)
    if path is None:
        return Probe(available=False, detail=f"{command}: not found or not executable")
    try:
        completed = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # A broken CLI gives a reason, never an exception: bytes that are
            # not UTF-8 are replaced instead of raising while decoding.
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Probe(
            available=False, detail=f"{path} --version: no answer within {timeout_seconds:g} s"
        )
    except OSError as exc:
        return Probe(available=False, detail=f"{path} --version: {exc}")
    if completed.returncode != 0:
        return Probe(available=False, detail=f"{path} --version exited {completed.returncode}")
    version = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), None)
    return Probe(available=True, detail=path, version=version)
