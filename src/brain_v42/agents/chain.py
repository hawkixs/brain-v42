"""One phase run across the configured provider CHAIN, with the Dream's log lines.

The state machine itself moved to :mod:`headless_agents.chain` (Brain ticket
b2a2d1a5): it advances on the two codes that prove no Brain tool call
succeeded -- ``PROVIDER_FALLBACK_EXIT_CODE`` (3, "failed, and I can prove no
Brain tool call succeeded") and ``TIMEOUT_REPLAYABLE_EXIT_CODE`` (4, "my
deadline fired on a stream that shows no Brain call ever started"). This
module keeps the signature ``run_phase_chain`` calls and the byte-identical
``FALLBACK``/``FALLBACK-END`` lines bash wrote (lot 2, ticket ``afd56820``),
and chooses the FALLBACK wording from the code that triggered the switch: a
link that expired empty did not fail, it never answered.
"""

from __future__ import annotations

from collections.abc import Callable

from headless_agents.chain import ChainResult as ChainResult
from headless_agents.chain import run_chain as _run_chain

from . import lines
from .capability import TIMEOUT_REPLAYABLE_EXIT_CODE


def run_chain(
    providers: list[str],
    *,
    run_one: Callable[[str], int],
    log: Callable[[str], None],
    project_key: str,
    phase: str,
) -> ChainResult:
    last_rc: dict[str, int] = {}

    def _run_one(provider: str) -> int:
        rc = run_one(provider)
        last_rc[provider] = rc
        return rc

    def _on_fallback(provider: str, next_provider: str) -> None:
        if last_rc.get(provider) == TIMEOUT_REPLAYABLE_EXIT_CODE:
            log(lines.dead_link_fallback_line(project_key, phase, provider, next_provider))
        else:
            log(lines.fallback_line(project_key, phase, provider, next_provider))

    return _run_chain(
        providers,
        run_one=_run_one,
        on_fallback=_on_fallback,
        on_exhausted=lambda provider: log(lines.fallback_end_line(project_key, phase, provider)),
    )
