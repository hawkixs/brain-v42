"""One run across a provider CHAIN: advance on proof, never on failure alone.

The chain moves to the next link on the single code
:data:`~headless_agents.capability.PROVIDER_FALLBACK_EXIT_CODE` (3), which
means "failed, and I can prove no tool call succeeded". An ordinary failure
(1) and a timeout (2) stop where they fell: neither proves nothing was
written, and replaying a run that mutated would make it write twice.

The chain reports, it does not log: ``on_fallback`` and ``on_exhausted`` are
the caller's hooks for whatever line its journal expects.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .capability import PROVIDER_FALLBACK_EXIT_CODE


@dataclass(frozen=True)
class ChainResult:
    provider: str
    rc: int
    fallbacks: tuple[str, ...]


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
    is none left, in which case the chain's rc is 1.
    """
    if not providers:
        raise ValueError("run_chain requires at least one provider")

    fallbacks: list[str] = []
    provider = providers[0]
    rc = 0
    for index, provider in enumerate(providers):
        rc = run_one(provider)

        if rc != PROVIDER_FALLBACK_EXIT_CODE:
            break

        if index + 1 < len(providers):
            next_provider = providers[index + 1]
            if on_fallback is not None:
                on_fallback(provider, next_provider)
            fallbacks.append(provider)
        else:
            if on_exhausted is not None:
                on_exhausted(provider)
            rc = 1

    return ChainResult(provider=provider, rc=rc, fallbacks=tuple(fallbacks))
