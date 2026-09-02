"""Every dream call carries TWO identities; only one was checked.

- The **TOKEN**: the server accepts a scoped principal only if
  `client_id == "dream-codex-{phase}"`, with an identical `agent` claim, the exact
  scopes and the exact claim set. That bound is tight, and it holds.
- The **ACTOR**: the `X-Brain-Agent` header, set as a context variable by
  `ProvenanceMiddleware`, **never checked against the token**.

This is NOT a scope hole — the capability bound is on the token. It is an
ATTRIBUTION defect, and it is STRUCTURAL, not accidental: the live drop-in
declares `BRAIN_DREAM_AGENT_PROVIDERS=codex,agy,claude`, and the three runners set
three distinct headers — `dream-codex-{phase}`, `dream-agy-{phase}`,
`dream-claude-{phase}` — against a token registry that mints codex profiles only.
**Two rails out of three diverge by construction, on every call.**

Measured on 2026-08-22 on `dream_runs`, the only surviving source: the codex rail
produced 672 runs since 08-04, the agy rail 235 between 08-11 and 08-17. The
fallback is not theoretical, it ran for a week.

WHY LOG RATHER THAN REFUSE. Refusing would break the fallback rail for as long as
it announces `agy` with a codex token. Deriving the actor from the token would
lose the information "which rail actually ran", precisely what made measuring the
ratio possible. Logging can break nothing and produces the denominator the other
two forms need — and there is NO other source: `access_log.actor` is drained
every 300 s (measured at 0 rows), nothing in journald carries the per-call actor,
`process_metrics.agent_name` is the PROCESS name, the collector's per-agent
counters live in memory and die at restart, and `dream_runs` has no actor column.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from brain_v42.provenance import set_current_actor
from tests.unit.mcp.test_dream_capabilities import (
    _call_context,
    _capability_middleware,
    _scoped_access_token,
)


def _divergences(events: list[tuple[str, dict]]) -> list[dict]:
    return [fields for event, fields in events if event == "dream_identity_divergence"]


@pytest.mark.asyncio
async def test_an_aligned_actor_is_silent() -> None:
    """The nominal codex rail must produce nothing — otherwise the signal is noise."""
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")
    seen: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event: str, **fields: Any) -> None:
            seen.append((event, fields))

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Recorder(), raising=False)
        set_current_actor("dream-codex-scan")
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result"
    assert _divergences(seen) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["dream-agy-scan", "dream-claude-scan", "unknown"])
async def test_a_diverging_actor_is_logged_AND_NOT_REFUSED(actor: str) -> None:
    """THE WITNESS THAT MATTERS: the call GOES THROUGH, and it is counted.

    Refusing would break the fallback rail, which is exactly what the fallback
    exists to avoid. An observation that blocks the night it observes is worse
    than the blind spot it corrects.
    """
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")
    seen: list[tuple[str, dict]] = []

    class _Recorder:
        def info(self, event: str, **fields: Any) -> None:
            seen.append((event, fields))

        def __getattr__(self, _name: str) -> Any:
            return lambda *a, **k: None

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Recorder(), raising=False)
        set_current_actor(actor)
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result", "la divergence ne refuse RIEN"
    call_next.assert_awaited_once()

    logged = _divergences(seen)
    assert len(logged) == 1
    fields = logged[0]
    assert fields["actor"] == actor
    assert fields["token_client_id"] == "dream-codex-scan"
    assert fields["phase"] == "scan"


@pytest.mark.asyncio
async def test_the_observation_can_never_break_the_call() -> None:
    """An OBSERVATION channel cannot be a point of failure.

    Same rule as `ProvenanceMiddleware._report`: at worst we lose one line.
    Without this witness, a logged divergence could kill the night it documents.
    """
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    middleware = _capability_middleware(capabilities)
    call_next = AsyncMock(return_value="allowed-result")

    class _Exploding:
        def __getattr__(self, _name: str) -> Any:
            def _boom(*_a: Any, **_k: Any) -> None:
                raise RuntimeError("journal cassé")

            return _boom

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            capabilities,
            "get_access_token",
            lambda: _scoped_access_token(phase="scan"),
            raising=False,
        )
        monkeypatch.setattr(capabilities, "logger", _Exploding(), raising=False)
        set_current_actor("dream-agy-scan")
        try:
            result = await middleware.on_call_tool(_call_context("brain_search"), call_next)
        finally:
            set_current_actor("unknown")

    assert result == "allowed-result"
