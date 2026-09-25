"""Tool counts per rail, from each rail's own event log (spec 0.5.0 §3.11).

The fixtures under ``fixtures/tool_counts/`` are recorded logs: real runs of each
rail, their structure kept verbatim and their free text redacted (plan Tasks 1-3).
"""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.event_log import read_events
from headless_agents.providers import codex, opencode

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
    )
    assert opencode.count_tools(log) == {}


def test_opencode_without_a_whole_log_is_not_measured(tmp_path: Path) -> None:
    assert opencode.count_tools(None) is None
    assert opencode.count_tools(_log(tmp_path, tail='{"type": "tool_u')) is None
    anonymous = _log(tmp_path, {"type": "tool_use", "part": {"type": "tool", "tool": "read"}})
    assert opencode.count_tools(anonymous) is None
