"""Tool counts per rail, from each rail's own event log (spec 0.5.0 §3.11).

The fixtures under ``fixtures/tool_counts/`` are recorded logs: real runs of each
rail, their structure kept verbatim and their free text redacted (plan Tasks 1-3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headless_agents.event_log import read_events
from headless_agents.providers import agy, codex, opencode
from headless_agents.registry import HTTP_PROVIDER_NAMES, UnknownProvider, tool_counts
from headless_agents.result import RunResult

FIXTURES = Path(__file__).parent / "fixtures" / "tool_counts"


def _log(tmp_path: Path, *events: object, tail: str = "") -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + tail, encoding="utf-8")
    return path


# ── the reader ──────────────────────────────────────────────────────────────


def test_read_events_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"type": "a"}\n\n   \n{"type": "b"}\n', encoding="utf-8")
    assert read_events(path) == [{"type": "a"}, {"type": "b"}]


def test_read_events_cannot_vouch_for_a_log_it_cannot_read_whole(tmp_path: Path) -> None:
    assert read_events(None) is None
    assert read_events(tmp_path / "absent.jsonl") is None
    assert read_events(_log(tmp_path, {"type": "a"}, tail='{"type": "item.star')) is None
    not_an_object = tmp_path / "list.jsonl"
    not_an_object.write_text("[1, 2]\n", encoding="utf-8")
    assert read_events(not_an_object) is None
    not_utf8 = tmp_path / "bytes.jsonl"
    not_utf8.write_bytes(b'{"type": "\xff"}\n')
    assert read_events(not_utf8) is None


def test_read_events_cannot_vouch_for_a_line_nested_too_deep(tmp_path: Path) -> None:
    """Final review of PR A: json.loads raises RecursionError, not ValueError, on deep
    nesting -- an MCP result can carry it, and it must not escape into the run's report."""
    deep = tmp_path / "deep.jsonl"
    deep.write_text("[" * 100_000 + "\n", encoding="utf-8")
    assert read_events(deep) is None


# ── codex ───────────────────────────────────────────────────────────────────


def test_codex_counts_a_recorded_run_by_item_type() -> None:
    """Four shell calls -- one failed with exit 2, still a call -- and two messages."""
    assert codex.count_tools(FIXTURES / "codex.events.jsonl") == {"command_execution": 4}


def test_codex_counts_an_item_once_across_its_events(tmp_path: Path) -> None:
    item = {
        "id": "item_1",
        "type": "mcp_tool_call",
        "server": "brain-v42",
        "tool": "brain_search",
        "arguments": {},
        "result": None,
        "error": None,
    }
    log = _log(
        tmp_path,
        {"type": "item.started", "item": {**item, "status": "in_progress"}},
        {"type": "item.updated", "item": {**item, "status": "in_progress"}},
        {"type": "item.completed", "item": {**item, "status": "completed"}},
    )
    assert codex.count_tools(log) == {"mcp_tool_call": 1}


def test_codex_counts_a_call_that_never_completed(tmp_path: Path) -> None:
    """A provider killed mid-call: the call started, so it may have run."""
    log = _log(
        tmp_path,
        {
            "type": "item.started",
            "item": {"id": "item_7", "type": "file_change", "status": "in_progress"},
        },
    )
    assert codex.count_tools(log) == {"file_change": 1}


def test_codex_excludes_messages_reasoning_and_errors(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": "<redacted>"},
        },
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "<redacted>"},
        },
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "error", "message": "<redacted>"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )
    assert codex.count_tools(log) == {}


def test_codex_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert codex.count_tools(None) is None
    assert codex.count_tools(tmp_path / "absent.jsonl") is None
    assert codex.count_tools(_log(tmp_path, tail='{"type": "item.started", "item": {"id"')) is None
    anonymous = _log(tmp_path, {"type": "item.started", "item": {"type": "command_execution"}})
    assert codex.count_tools(anonymous) is None


# ── opencode ────────────────────────────────────────────────────────────────


def test_opencode_counts_a_recorded_run_by_tool() -> None:
    assert opencode.count_tools(FIXTURES / "opencode.events.jsonl") == {"grep": 1, "glob": 1}


def test_opencode_counts_a_part_once(tmp_path: Path) -> None:
    part = {
        "type": "tool",
        "tool": "read",
        "callID": "call_1",
        "id": "prt_1",
        "state": {"status": "running"},
    }
    log = _log(
        tmp_path,
        {"type": "tool_use", "part": part},
        {"type": "tool_use", "part": {**part, "state": {"status": "completed"}}},
        {"type": "tool_use", "part": {**part, "id": "prt_2", "callID": "call_2", "tool": "edit"}},
    )
    assert opencode.count_tools(log) == {"read": 1, "edit": 1}


def test_opencode_ignores_steps_and_text(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {"type": "step_start", "part": {"id": "prt_1", "type": "step-start"}},
        {"type": "text", "part": {"id": "prt_2", "type": "text", "text": "<redacted>"}},
        {"type": "step_finish", "part": {"id": "prt_3", "type": "step-finish", "reason": "stop"}},
    )
    assert opencode.count_tools(log) == {}


def test_opencode_step_cut_before_its_finish_is_not_measured(tmp_path: Path) -> None:
    """Final review of PR A: opencode logs a call only once it settles, so a step
    killed before its step_finish may hold a call in flight -- not measured."""
    recorded = (FIXTURES / "opencode.events.jsonl").read_text(encoding="utf-8").splitlines()
    cut = tmp_path / "events.jsonl"
    cut.write_text("\n".join(recorded[:3]) + "\n", encoding="utf-8")
    assert opencode.count_tools(cut) is None


def test_opencode_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert opencode.count_tools(None) is None
    assert opencode.count_tools(_log(tmp_path, tail='{"type": "tool_u')) is None
    anonymous = _log(tmp_path, {"type": "tool_use", "part": {"type": "tool", "tool": "read"}})
    assert opencode.count_tools(anonymous) is None


# ── agy ─────────────────────────────────────────────────────────────────────


def test_agy_counts_a_recorded_run_by_tool_name() -> None:
    """One MCP call that succeeded; a file read and a command the guard refused."""
    assert agy.count_tools(FIXTURES / "agy.events.jsonl") == {
        "call_mcp_tool": 1,
        "view_file": 1,
        "run_command": 1,
    }


def test_agy_counts_a_step_once_across_its_states(tmp_path: Path) -> None:
    step = {"conversation_id": "c1", "step_index": 4, "step_type": "tool", "tool_name": "view_file"}
    log = _log(
        tmp_path,
        {"event": "step_update", "step_update": {**step, "state": "ACTIVE"}},
        {"event": "step_update", "step_update": {**step, "state": "DONE"}},
        {"event": "step_update", "step_update": {**step, "step_index": 6, "state": "ACTIVE"}},
    )
    assert agy.count_tools(log) == {"view_file": 2}


def test_agy_counts_two_conversations_sharing_a_step_index_as_two_calls(tmp_path: Path) -> None:
    """Codex closure of this plan (carry-forward): the identity is the pair, not the index."""
    step = {"step_index": 4, "step_type": "tool", "tool_name": "view_file", "state": "ACTIVE"}
    log = _log(
        tmp_path,
        {"event": "step_update", "step_update": {**step, "conversation_id": "c1"}},
        {"event": "step_update", "step_update": {**step, "conversation_id": "c2"}},
    )
    assert agy.count_tools(log) == {"view_file": 2}


def test_agy_ignores_steps_that_are_not_tools(tmp_path: Path) -> None:
    log = _log(
        tmp_path,
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "c1",
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
            },
        },
    )
    assert agy.count_tools(log) == {}


def test_agy_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert agy.count_tools(None) is None
    assert agy.count_tools(_log(tmp_path, tail='{"event": "step_up')) is None
    unindexed = _log(
        tmp_path,
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "c1",
                "step_type": "tool",
                "tool_name": "view_file",
                "state": "ACTIVE",
            },
        },
    )
    assert agy.count_tools(unindexed) is None


def test_agy_without_a_conversation_id_is_not_measured(tmp_path: Path) -> None:
    """Codex review of this plan (round 1): half an identity would merge two calls."""
    anonymous = _log(
        tmp_path,
        {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "step_type": "tool",
                "tool_name": "view_file",
                "state": "ACTIVE",
            },
        },
    )
    assert agy.count_tools(anonymous) is None


# ── the facade ──────────────────────────────────────────────────────────────


def _result(provider: str, events_log: Path | None, raw_log: Path | None = None) -> RunResult:
    return RunResult(
        exit_code=0,
        provider=provider,
        model="m",
        report_path=None,
        events_log=events_log,
        raw_log=raw_log,
        tokens=None,
        duration_seconds=1.0,
        tool_call_completed=False,
    )


def test_the_facade_reaches_each_rails_counter() -> None:
    assert tool_counts(_result("codex", FIXTURES / "codex.events.jsonl")) == {
        "command_execution": 4
    }
    assert tool_counts(_result("opencode", FIXTURES / "opencode.events.jsonl")) == {
        "grep": 1,
        "glob": 1,
    }
    assert tool_counts(_result("agy", FIXTURES / "agy.events.jsonl")) == {
        "call_mcp_tool": 1,
        "view_file": 1,
        "run_command": 1,
    }


@pytest.mark.parametrize("provider", sorted(HTTP_PROVIDER_NAMES))
def test_an_http_provider_has_no_tools(provider: str) -> None:
    assert tool_counts(_result(provider, None)) == {}


def test_claude_is_not_measured_even_with_telemetry(tmp_path: Path) -> None:
    """Plan P3: null until a live test proves its telemetry complete at exit."""
    raw = tmp_path / "raw.log"
    raw.write_text(
        'body: "claude_code.tool_result"\nattributes: { tool_name: "Read", success: "true" }\n'
    )
    assert tool_counts(_result("claude", tmp_path / "events.jsonl", raw)) is None


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(UnknownProvider):
        tool_counts(_result("nope", None))
