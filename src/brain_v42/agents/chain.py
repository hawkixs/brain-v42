"""One phase run across the configured provider CHAIN, with the Dream's log lines.

The state machine itself moved to :mod:`headless_agents.chain` (Brain ticket
b2a2d1a5): it advances to the next link on the single code
``PROVIDER_FALLBACK_EXIT_CODE`` (3), which means "failed, and I can prove no
Brain tool call succeeded". This module keeps the signature ``run_phase_chain``
calls and the byte-identical ``FALLBACK``/``FALLBACK-END`` lines bash wrote
(lot 2, ticket ``afd56820``).
"""

from __future__ import annotations

from collections.abc import Callable

from headless_agents.chain import ChainResult as ChainResult
from headless_agents.chain import run_chain as _run_chain

from . import lines


def run_chain(
    providers: list[str],
    *,
    run_one: Callable[[str], int],
    log: Callable[[str], None],
    project_key: str,
    phase: str,
) -> ChainResult:
    return _run_chain(
        providers,
        run_one=run_one,
        on_fallback=lambda provider, next_provider: log(
            lines.fallback_line(project_key, phase, provider, next_provider)
        ),
        on_exhausted=lambda provider: log(lines.fallback_end_line(project_key, phase, provider)),
    )
