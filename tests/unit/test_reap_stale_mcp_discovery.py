"""Tests for the (pure) process-discovery half of the stale-MCP reaper.

These cover turning a ``ps`` snapshot into :class:`McpServerProc` records:
parsing ps lines, recognising *only genuine* brain MCP server processes (not
greps/tails/editors that merely mention the module), resolving each server to
its owning agent by walking the parent chain, and extracting the real
``claude --print`` parent so the CLI can terminate the whole abandoned session.
"""

from __future__ import annotations

from brain_v42.maintenance.reap_stale_mcp import (
    build_mcp_server_procs,
    is_brain_server_cmd,
    parse_ps_snapshot,
    resolve_agent_key,
)

# Real-world tree (from the 2026-06-29 incident):
#   uv run shrik (5826) -> shrik.b (5914) -> claude remote-control --name X
#     -> claude --print --sdk-url ... -> python -m brain_v42.mcp.server
SNAPSHOT = {
    5826: (1, "/home/h/.local/bin/uv run --project /red/red-shrik -m shrik"),
    5914: (5826, "/red/red-shrik/.venv/bin/python3 -m shrik.b"),
    39859: (5914, "/home/h/.local/bin/claude remote-control --name brain-v42"),
    1493626: (39859, "/home/h/.local/share/claude/versions/2.1.186 --print --sdk-url https://x"),
    1493644: (1493626, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server"),
    # shrik-direct brain server (no claude layer)
    3747829: (1, "/home/h/.local/bin/uv run --project /red/red-shrik -m shrik"),
    3747833: (3747829, "/red/red-shrik/.venv/bin/python3 -m shrik --config /h/x"),
    1524010: (3747833, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server"),
}


def test_is_brain_server_cmd_matches_real_invocations() -> None:
    assert is_brain_server_cmd("/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server")
    assert is_brain_server_cmd("python3.14 -m brain_v42.mcp.server")
    assert is_brain_server_cmd("/usr/bin/uv run python -m brain_v42.mcp.server --foo")


def test_is_brain_server_cmd_rejects_lookalikes() -> None:
    # processes that merely MENTION the module must not be classified as servers
    assert not is_brain_server_cmd("grep -rn brain_v42.mcp.server src/")
    assert not is_brain_server_cmd("tail -f /var/log/brain_v42.mcp.server.log")
    assert not is_brain_server_cmd("vim src/brain_v42/mcp/server.py")
    # the reaper itself runs a DIFFERENT module
    assert not is_brain_server_cmd(
        "/red/brain_v42/.venv/bin/python -m brain_v42.maintenance.reap_stale_mcp"
    )


def test_resolve_agent_key_finds_remote_control_name() -> None:
    key, print_pid = resolve_agent_key(1493644, SNAPSHOT)
    assert key == "remote-control:brain-v42"
    assert print_pid == 1493626  # the claude --print parent to also terminate


def test_resolve_agent_key_shrik_direct_has_no_print_parent() -> None:
    key, print_pid = resolve_agent_key(1524010, SNAPSHOT)
    assert key == "shrik-worker:3747833"
    assert print_pid is None  # never terminate the shrik worker itself


def test_resolve_agent_key_unknown_parent_falls_back_to_parent_pid() -> None:
    snap = {999: (4242, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server")}
    key, print_pid = resolve_agent_key(999, snap)
    assert key == "parent:4242"
    assert print_pid is None


def test_resolve_agent_key_does_not_treat_print_substring_as_claude_print() -> None:
    """A non-claude parent with '--print-debug' must not be flagged for SIGTERM."""
    snap = {
        10: (1, "/red/red-shrik/.venv/bin/python3 -m shrik"),
        11: (10, "/opt/tool --print-debug --format json"),
        12: (11, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server"),
    }
    key, print_pid = resolve_agent_key(12, snap)
    assert print_pid is None  # '--print-debug' is not a claude --print session
    assert key == "parent:11"


def test_parse_ps_snapshot_builds_pid_ppid_cmd_and_ages() -> None:
    lines = [
        "    PID    PPID   ELAPSED COMMAND",
        "   1493644 1493626    430 /red/brain_v42/.venv/bin/python -m brain_v42.mcp.server",
        "   1493626   39859   3000 claude --print --sdk-url https://x",
    ]
    snapshot, ages = parse_ps_snapshot(lines)
    assert snapshot[1493644] == (1493626, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server")
    assert snapshot[1493626][0] == 39859
    assert ages[1493644] == 430.0
    assert 1493626 in ages


def test_build_mcp_server_procs_only_includes_brain_servers() -> None:
    ages = {1493644: 430.0, 1493626: 3000.0, 1524010: 80000.0, 3747833: 90000.0}
    procs = build_mcp_server_procs(SNAPSHOT, ages)
    pids = {p.pid for p in procs}
    # only the two brain MCP servers, not claude / shrik / uv processes
    assert pids == {1493644, 1524010}
    by_pid = {p.pid: p for p in procs}
    assert by_pid[1493644].agent_key == "remote-control:brain-v42"
    assert by_pid[1493644].age_sec == 430.0
    assert by_pid[1493644].parent_print_pid == 1493626
    assert by_pid[1524010].agent_key == "shrik-worker:3747833"
    assert by_pid[1524010].parent_print_pid is None


def test_build_mcp_server_procs_excludes_marker_lookalikes() -> None:
    """A grep/tail that mentions the module is NOT built as a server."""
    snap = {
        50: (1, "grep -rn brain_v42.mcp.server src/"),
        51: (1, "tail -f /var/log/brain_v42.mcp.server.log"),
        54: (1, "/red/brain_v42/.venv/bin/python -m brain_v42.mcp.server"),
    }
    ages = {50: 99999.0, 51: 99999.0, 54: 99999.0}
    procs = build_mcp_server_procs(snap, ages)
    assert {p.pid for p in procs} == {54}
