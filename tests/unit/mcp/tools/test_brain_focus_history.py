"""The reading side of M-D: what the trail says, and what it refuses to hide.

`focus_diff` is a pure function and is tested as one. The tool is tested through
a stub service, because what is interesting here is the RENDERING — an erased
focus that must not read as a gap, and a copy-forward that must be marked rather
than filtered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from brain_v42.db.focus_history import focus_diff
from brain_v42.mcp.tools.project_context_tools import register_project_context_tools


def _row(revision: int, focus: str | None, **overrides: Any) -> dict[str, Any]:
    return {
        "focus_revision": revision,
        "focus": focus,
        "actor": None,
        "source": "focus_tool",
        "created_at": datetime(2026, 9, 2, tzinfo=UTC),
        **overrides,
    }


def _tool(rows: list[dict[str, Any]]) -> Any:
    """Register the tools against a stub mcp and hand back `brain_focus_history`."""
    captured: dict[str, Any] = {}

    class _Mcp:
        def tool(self, **_kwargs: Any) -> Any:
            def decorate(fn: Any) -> Any:
                captured[fn.__name__] = fn
                return fn

            return decorate

    service = MagicMock()
    service.focus_history = AsyncMock(return_value=rows)
    return _tool_with(service)


def _tool_with(service: Any) -> Any:
    captured: dict[str, Any] = {}

    class _Mcp:
        def tool(self, **_kwargs: Any) -> Any:
            def decorate(fn: Any) -> Any:
                captured[fn.__name__] = fn
                return fn

            return decorate

    register_project_context_tools(_Mcp(), service)
    return captured["brain_focus_history"]


# ── focus_diff, the pure part ────────────────────────────────────────────────


def test_the_diff_counts_characters_in_both_directions() -> None:
    assert focus_diff("abc", "abcdef") == {"added": 3, "removed": 0, "unchanged": False}
    assert focus_diff("abcdef", "abc") == {"added": 0, "removed": 3, "unchanged": False}


def test_an_erasure_is_a_removal_not_an_absence() -> None:
    """A focus overwritten to NULL is the destructive move the trail exists for."""
    assert focus_diff("prose", None) == {"added": 0, "removed": 5, "unchanged": False}


def test_a_copy_forward_is_flagged_rather_than_measured_at_zero() -> None:
    """`unchanged` is not "added == removed == 0" restated.

    A session close re-posting the previous prose verbatim is the NORMAL regime:
    the CAS sets `expected + 1` without comparing the text. Distinguishing that
    from a same-length rewrite is the difference between a trail a human reads
    and one they learn to skim.
    """
    assert focus_diff("same", "same")["unchanged"] is True
    assert focus_diff("abcd", "wxyz")["unchanged"] is False


# ── the tool's rendering ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_trail_says_so_rather_than_rendering_a_header() -> None:
    assert await _tool([])(project_key="brain-v42") == "No focus history for brain-v42."


@pytest.mark.asyncio
async def test_an_erased_focus_renders_as_erased_not_as_a_blank() -> None:
    """A blank line would read as "the tool lost it", which is the opposite of the point."""
    rendered = await _tool([_row(4, None), _row(3, "prose worth keeping")])(project_key="brain-v42")

    assert "(erased)" in rendered
    assert "-19 chars" in rendered, "the erasure is measured against what it replaced"


@pytest.mark.asyncio
async def test_a_copy_forward_is_marked_in_the_listing() -> None:
    rendered = await _tool([_row(8, "carried"), _row(7, "carried")])(project_key="brain-v42")

    assert "unchanged" in rendered


@pytest.mark.asyncio
async def test_the_oldest_row_on_the_page_claims_no_diff_it_cannot_compute() -> None:
    """The bottom row has no predecessor HERE — inventing "+N/-0" would invent an event.

    It may simply be the end of the page rather than the first revision, which is
    why the marker distinguishes the two.
    """
    rendered = await _tool([_row(1, "second"), _row(0, "first")])(project_key="brain-v42")

    lines = [line for line in rendered.splitlines() if line.startswith("- r")]
    assert "first recorded revision" in lines[-1]
    assert "chars" not in lines[-1]


@pytest.mark.asyncio
async def test_a_malformed_project_key_is_refused_before_any_read() -> None:
    """`format_error` RAISES in this repo — the refusal reaches the client as an error.

    Asserting the raise rather than a returned string is what keeps this honest:
    a tool that returned the message would look identical here and would be read
    by a client as a successful answer.
    """
    service = MagicMock()
    service.focus_history = AsyncMock(return_value=[_row(0, "x")])
    tool = _tool_with(service)

    with pytest.raises(ToolError, match="kebab-case"):
        await tool(project_key="Not A Key")

    service.focus_history.assert_not_awaited(), "refused BEFORE the read"
