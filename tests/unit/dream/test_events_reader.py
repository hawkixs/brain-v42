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
from scripts.dream import events_reader
from scripts.dream.events_reader import (
    AGY,
    CODEX,
    UnknownDialectError,
    _Counts,
    emptiness_unknown,
    format_census_report,
    is_empty_search,
    iter_tool_calls,
    run_census,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dream_events"
_AGY_FILE = _FIXTURES / "2026-08-12_demo-project_scan.events.jsonl"
_CODEX_FILE = _FIXTURES / "2026-09-05_demo-project_scan.events.jsonl"
_CODEX_CONNECT_FILE = _FIXTURES / "2026-09-05_demo-project_connect.events.jsonl"
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


def test_iter_tool_calls_raises_eagerly_not_only_when_the_iterator_is_advanced() -> None:
    """``iter_tool_calls`` must not be a generator function.

    A generator function only starts executing its body -- and so only
    raises -- once its returned iterator is first advanced. A caller
    wrapping the call itself in ``try/except UnknownDialectError`` (rather
    than the eventual ``list(...)``/``for`` loop) must see the error here.
    """
    with pytest.raises(UnknownDialectError):
        iter_tool_calls(_UNKNOWN_FILE)


def test_agy_successful_search_with_no_recorded_output_is_flagged_unknown() -> None:
    """A DONE, non-error agy ``brain_search`` call can still omit ``tool_info.output``.

    Measured on the real corpus: 105 of 694 agy ``brain_search`` calls are in
    this state. Such a call must never be scored as "not empty" by
    :func:`is_empty_search` without a companion signal saying its emptiness
    was never actually measured.
    """
    call = next(
        c
        for c in iter_tool_calls(_AGY_FILE)
        if c.tool == "brain_search" and not c.is_error and c.output is None
    )
    assert emptiness_unknown(call) is True
    assert is_empty_search(call) is False


def test_emptiness_unknown_rejects_error_calls_and_non_search_tools() -> None:
    calls = list(iter_tool_calls(_AGY_FILE))
    error_call = next(c for c in calls if c.is_error)
    non_search_call = next(c for c in calls if c.tool != "brain_search")
    assert emptiness_unknown(error_call) is False
    assert emptiness_unknown(non_search_call) is False


def test_run_census_reports_unknown_emptiness_separately_from_empties() -> None:
    report = run_census(logs_dir=_FIXTURES, night="2026-08-12", tool_filter="brain_search")
    assert report.total_unknown == 1
    assert report.calls_by_phase["scan"].unknown == 1
    # The genuinely empty search is still counted as an empty, not folded
    # into "unknown".
    assert report.total_empties == 1

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "unknown=1" in text
    assert "UNMEASURED emptiness: 1 call(s)" in text


def test_run_census_zero_fills_phases_with_no_matching_calls_under_a_tool_filter() -> None:
    """A phase whose file was parsed but had zero matching calls still renders.

    Reproduces the W11-investigation confusion where the CLEAN phase vanished
    from a ``--tool brain_search`` census: here the classified ``connect``
    file has no ``brain_search`` call at all, and must still show
    ``calls=0`` rather than being absent from the report.
    """
    assert _CODEX_CONNECT_FILE.exists()

    report = run_census(logs_dir=_FIXTURES, night="2026-09-05", tool_filter="brain_search")

    assert "connect" in report.calls_by_phase
    assert report.calls_by_phase["connect"] == _Counts()

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "connect: calls=0 empties=0 unknown=0" in text


def test_run_census_loads_each_classified_file_exactly_once(monkeypatch) -> None:  # noqa: ANN001
    """``run_census`` must not parse the same file's JSON twice.

    Before the fix, ``detect_dialect(path)`` and ``iter_tool_calls(path)``
    each called the private loader independently for the very same path.
    """
    load_calls: list[Path] = []
    original_load_records = events_reader._load_records

    def counting_load_records(path: Path) -> list[dict]:  # noqa: ANN001
        load_calls.append(path)
        return original_load_records(path)

    monkeypatch.setattr(events_reader, "_load_records", counting_load_records)

    run_census(logs_dir=_FIXTURES, night="2026-08-12")

    assert load_calls == [_AGY_FILE]
