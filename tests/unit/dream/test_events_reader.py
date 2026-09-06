"""Both dream wire dialects normalise to the same ``ToolCall`` shape.

The dream pool has written two incompatible event shapes to
``logs/dream/*.events.jsonl`` since the codex/agy rail split: the codex rail
(``codex exec --json``, live default) emits ``item.started``/``item.completed``
records, while the agy rail (``agy --output-format stream-json``, older/
fallback path) emits ``step_update`` records with the tool output nested
under ``tool_info.output``. A census that reads only one shape silently drops
every call written by the other rail — this module's fixtures pin that both
shapes normalise identically for the SAME logical call, so a regression in
either branch of the reader shows up here rather than in a night's totals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.dream.events_reader import (
    AGY,
    CODEX,
    UnknownDialectError,
    is_empty_search,
    iter_tool_calls,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dream_events"
_AGY_FILE = _FIXTURES / "2026-08-12_demo-project_scan.events.jsonl"
_CODEX_FILE = _FIXTURES / "2026-09-05_demo-project_scan.events.jsonl"
_UNKNOWN_FILE = _FIXTURES / "2026-09-05_demo-project_mystery.events.jsonl"
_EMPTY_FILE = _FIXTURES / "2026-09-05_demo-project_empty.events.jsonl"

_SEARCH_OUTPUT = (
    '## 2 results for "clean phase status" (across all types)\n\n'
    "### Learnings (2)\n"
    "[s:0.91] 1. **Fixture learning** [low] (id:11111111-1111-1111-1111-111111111111)\n"
    "   project:demo-project | tags: dream:generated"
)
_EMPTY_OUTPUT = '## 0 results for "nonexistent fixture query" (across all types)'


def test_agy_fixture_parses_as_the_agy_dialect() -> None:
    calls = list(iter_tool_calls(_AGY_FILE))
    assert calls
    assert all(call.dialect == AGY for call in calls)


def test_codex_fixture_parses_as_the_codex_dialect() -> None:
    calls = list(iter_tool_calls(_CODEX_FILE))
    assert calls
    assert all(call.dialect == CODEX for call in calls)


def test_both_dialects_normalise_the_same_logical_search_call_identically() -> None:
    """The non-empty ``brain_search`` call is byte-identical across dialects, dialect field excepted."""
    agy_call = next(
        c for c in iter_tool_calls(_AGY_FILE) if c.tool == "brain_search" and not is_empty_search(c)
    )
    codex_call = next(
        c
        for c in iter_tool_calls(_CODEX_FILE)
        if c.tool == "brain_search" and not is_empty_search(c)
    )

    assert agy_call.phase == codex_call.phase == "scan"
    assert agy_call.project == codex_call.project == "demo-project"
    assert agy_call.tool == codex_call.tool == "brain_search"
    assert (
        agy_call.arguments
        == codex_call.arguments
        == {
            "query": "clean phase status",
            "limit": 5,
        }
    )
    assert agy_call.output == codex_call.output == _SEARCH_OUTPUT
    assert agy_call.is_error is False
    assert codex_call.is_error is False
    assert agy_call.dialect == AGY
    assert codex_call.dialect == CODEX


def test_empty_search_detected_identically_on_both_dialects() -> None:
    agy_empty = next(c for c in iter_tool_calls(_AGY_FILE) if is_empty_search_candidate(c))
    codex_empty = next(c for c in iter_tool_calls(_CODEX_FILE) if is_empty_search_candidate(c))

    assert agy_empty.output == codex_empty.output == _EMPTY_OUTPUT
    assert is_empty_search(agy_empty) is True
    assert is_empty_search(codex_empty) is True


def is_empty_search_candidate(call) -> bool:  # noqa: ANN001 - tiny local helper
    return call.tool == "brain_search" and call.output == _EMPTY_OUTPUT


def test_is_empty_search_rejects_non_search_tools() -> None:
    non_search_calls = [c for c in iter_tool_calls(_AGY_FILE) if c.tool != "brain_search"]
    assert non_search_calls
    for call in non_search_calls:
        assert is_empty_search(call) is False


def test_is_empty_search_rejects_error_results() -> None:
    error_calls = [c for c in iter_tool_calls(_AGY_FILE) if c.is_error]
    assert error_calls
    for call in error_calls:
        assert is_empty_search(call) is False


def test_agy_error_call_is_flagged_and_carries_the_validation_message() -> None:
    error_call = next(c for c in iter_tool_calls(_AGY_FILE) if c.tool == "brain_list")
    assert error_call.is_error is True
    assert error_call.output is not None
    assert "Missing required argument" in error_call.output


def test_codex_error_call_is_flagged_with_no_result_payload() -> None:
    error_call = next(c for c in iter_tool_calls(_CODEX_FILE) if c.tool == "list_mcp_resources")
    assert error_call.is_error is True
    assert error_call.output == "resources/list failed: unknown MCP server 'brain_v42'"


def test_unknown_dialect_file_raises_a_named_error() -> None:
    with pytest.raises(UnknownDialectError):
        list(iter_tool_calls(_UNKNOWN_FILE))


def test_empty_file_raises_a_named_error_rather_than_a_silent_zero() -> None:
    with pytest.raises(UnknownDialectError):
        list(iter_tool_calls(_EMPTY_FILE))
