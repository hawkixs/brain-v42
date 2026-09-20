"""One run across a provider CHAIN: advance on proof, never on failure alone.

The chain moves to the next link on two codes only, both of which mean "I
can prove no tool call succeeded":
:data:`~headless_agents.capability.PROVIDER_FALLBACK_EXIT_CODE` (3, a failure
with that proof) and :data:`~headless_agents.capability.TIMEOUT_REPLAYABLE_EXIT_CODE`
(4, the runner's own deadline on a stream that shows no call ever started).
An ordinary failure (1) and a plain timeout (2 or 124) stop where they fell:
neither proves nothing was written, and replaying a run that mutated would
make it write twice.

A 4 says one more thing than a 3: the link did not answer for a whole
deadline. The chain reports it in ``dead_links`` so the caller can decide not
to offer that link the next run -- the chain itself never remembers across
runs. A 3 can be transient (one refused call, one bad launch) and is never
counted as dead.

The chain reports, it does not log: ``on_fallback`` and ``on_exhausted`` are
the caller's hooks for whatever line its journal expects.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .capability import FALLBACK_EXIT_CODES, TIMEOUT_REPLAYABLE_EXIT_CODE


@dataclass(frozen=True)
class ChainResult:
    provider: str
    rc: int
    fallbacks: tuple[str, ...]
    dead_links: tuple[str, ...] = ()


def run_chain(
    providers: Sequence[str],
    *,
    run_one: Callable[[str], int],
    on_fallback: Callable[[str, str], None] | None = None,
    on_exhausted: Callable[[str], None] | None = None,
) -> ChainResult:
    """Run ``run_one`` on each provider in turn until one does not ask for a fallback.

    ``on_fallback(provider, next_provider)`` fires before each switchover;
    ``on_exhausted(provider)`` fires when the last link asked for one and there
    is none left, in which case the chain's rc is 1. ``dead_links`` names every
    link that returned the replayable-timeout code, last link included.
    """
    if not providers:
        raise ValueError("run_chain requires at least one provider")

    fallbacks: list[str] = []
    dead_links: list[str] = []
    provider = providers[0]
    rc = 0
    for index, provider in enumerate(providers):
        rc = run_one(provider)

        if rc not in FALLBACK_EXIT_CODES:
            break
        if rc == TIMEOUT_REPLAYABLE_EXIT_CODE:
            dead_links.append(provider)

        if index + 1 < len(providers):
            next_provider = providers[index + 1]
            if on_fallback is not None:
                on_fallback(provider, next_provider)
            fallbacks.append(provider)
        else:
            if on_exhausted is not None:
                on_exhausted(provider)
            rc = 1

    return ChainResult(
        provider=provider, rc=rc, fallbacks=tuple(fallbacks), dead_links=tuple(dead_links)
    )
