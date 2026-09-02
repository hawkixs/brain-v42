"""Server shutdown closes the activity emitter — the loss enters the counter.

Ticket `d5e4bd73`, second hole: `close()` existed and was wired NOWHERE — the
in-flight POSTs died at shutdown without being counted (~2.2/day, negligible in
volume; what matters is that the loss was not in the loss counter, exactly the
defect of the main hole). The wiring goes through `close_activity_reporter()`,
registered in `app_lifecycle`'s AsyncExitStack — the single owner of both
transports' lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from brain_v42.mcp import activity_reporter as reporter_module
from brain_v42.mcp.activity_reporter import (
    close_activity_reporter,
    set_activity_reporter,
)

SERVER_SOURCE = (Path(__file__).parents[3] / "src" / "brain_v42" / "mcp" / "server.py").read_text(
    encoding="utf-8"
)


@pytest.fixture(autouse=True)
def _reset_reporter() -> None:
    set_activity_reporter(None)
    yield
    set_activity_reporter(None)


@pytest.mark.asyncio
async def test_close_drains_then_forgets_the_reporter() -> None:
    fake = AsyncMock()
    set_activity_reporter(fake)

    await close_activity_reporter()

    fake.close.assert_awaited_once()
    # A closed emitter must never be reused: a POST after aclose() would raise on
    # the hot path of a tool call.
    assert reporter_module._reporter is None


@pytest.mark.asyncio
async def test_close_without_a_reporter_is_a_quiet_no_op() -> None:
    await close_activity_reporter()


@pytest.mark.asyncio
async def test_a_failing_close_never_breaks_the_shutdown() -> None:
    """Same promise as the emitter: observation is never the failure — here, a
    sidecar dead at shutdown must not make the shutdown fail."""
    fake = AsyncMock()
    fake.close.side_effect = RuntimeError("sidecar déjà mort")
    set_activity_reporter(fake)

    await close_activity_reporter()

    assert reporter_module._reporter is None


def test_the_lifecycle_registers_the_close() -> None:
    """The wiring lives in `app_lifecycle`, not in a convention: a transport added
    tomorrow inherits the close without thinking about it."""
    assert "cleanup.push_async_callback(close_activity_reporter)" in SERVER_SOURCE


def test_no_docstring_still_claims_close_is_unwired() -> None:
    """The two comments that justified the absence of wiring must fall with it — a
    text describing the old world would lead to the same decision being taken again
    for the wrong reasons."""
    reporter_source = (
        Path(__file__).parents[3] / "src" / "brain_v42" / "mcp" / "activity_reporter.py"
    ).read_text(encoding="utf-8")

    assert "câblé NULLE PART" not in reporter_source
    assert "Aucune fermeture n'est câblée" not in reporter_source
