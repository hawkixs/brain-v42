"""One phase run across the configured provider CHAIN.

The Python replacement of ``scripts/dream.sh``'s ``run_phase_chain`` (lot 2 of
the agent runtime extraction, Brain ticket ``afd56820``).

It advances to the next link on the single code ``PROVIDER_FALLBACK_EXIT_CODE``
(3), which means "failed, and I can prove no Brain tool call succeeded". An
ordinary failure (1) and a timeout (2) stop where they fell: neither proves
nothing was written, and replaying a phase that mutated would make it write
twice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import lines
from .capability import PROVIDER_FALLBACK_EXIT_CODE


@dataclass(frozen=True)
class ChainResult:
    provider: str
    rc: int
    fallbacks: tuple[str, ...]


def run_chain(
    providers: list[str],
    *,
    run_one: Callable[[str], int],
    log: Callable[[str], None],
    project_key: str,
    phase: str,
) -> ChainResult:
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
            log(lines.fallback_line(project_key, phase, provider, next_provider))
            fallbacks.append(provider)
        else:
            log(lines.fallback_end_line(project_key, phase, provider))
            rc = 1

    return ChainResult(provider=provider, rc=rc, fallbacks=tuple(fallbacks))
