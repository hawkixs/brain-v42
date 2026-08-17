"""Tests for the stale-MCP-session reaper selection logic.

The reaper exists to prevent the recurring "too many clients" incident on
brain_v42_postgres: red-shrik's remote-control agent fleet spawns a fresh
``claude --print`` session (each with its own brain MCP server + asyncpg pool)
per invocation, and the OLD sessions linger instead of exiting, accumulating
~5 PG connections each until max_connections=100 is saturated.

Selection rule (pure, testable here):
- Group brain MCP servers by their owning agent (remote-control name / shrik
  worker pid).
- Within each agent group, the NEWEST server (smallest age; higher PID breaks
  ties) is the active session and is protected -- UNLESS it exceeds the absolute
  cap ``max_age_sec`` (owner policy: any session older than 48h is cut, even the
  only/newest one of its agent -- no legitimate session runs that long).
- Reap the older siblings of an agent once they exceed ``min_age_sec`` (never
  touch anything younger -- protects in-flight handoffs and fresh work).
- The reaper's own process tree is protected separately in ``plan_and_reap`` via
  ``protect_pid``, so the absolute cap never kills the reaper's own session.
"""

from __future__ import annotations

from brain_v42.maintenance.reap_stale_mcp import (
    McpServerProc,
    select_stale_mcp_sessions,
)


def _proc(pid: int, age_sec: float, agent_key: str, parent_print_pid=None):
    return McpServerProc(
        pid=pid, age_sec=age_sec, agent_key=agent_key, parent_print_pid=parent_print_pid
    )


def test_single_server_per_agent_is_never_reaped() -> None:
    """One server for an agent (under the cap) is the active session."""
    procs = [_proc(100, age_sec=6 * 3600, agent_key="remote-control:brain-v42")]
    assert select_stale_mcp_sessions(procs) == []


def test_older_sibling_of_same_agent_is_reaped_newest_kept() -> None:
    """Two sessions for one agent: keep the newest, reap the older one."""
    newest = _proc(200, age_sec=45 * 60, agent_key="remote-control:red-codex")
    older = _proc(201, age_sec=3 * 3600, agent_key="remote-control:red-codex")
    reaped = select_stale_mcp_sessions([newest, older])
    assert reaped == [older]


def test_young_sibling_is_protected_even_if_not_newest() -> None:
    """Nothing younger than min_age_sec is ever reaped (handoff safety)."""
    newest = _proc(300, age_sec=2 * 60, agent_key="remote-control:ReD")
    young_sibling = _proc(301, age_sec=10 * 60, agent_key="remote-control:ReD")
    # default min_age_sec = 1800 (30 min); both younger -> none reaped
    assert select_stale_mcp_sessions([newest, young_sibling]) == []


def test_different_agents_are_independent_groups() -> None:
    """An old session for agent A does not affect agent B's single session."""
    a_new = _proc(400, age_sec=20 * 60, agent_key="remote-control:red-watcher")
    a_old = _proc(401, age_sec=5 * 3600, agent_key="remote-control:red-watcher")
    b_solo = _proc(402, age_sec=8 * 3600, agent_key="remote-control:red-games")
    reaped = select_stale_mcp_sessions([a_new, a_old, b_solo])
    assert reaped == [a_old]


def test_newest_protected_when_older_than_min_age_but_under_cap() -> None:
    """A long-running active session (newest, under the 48h cap) stays protected."""
    newest = _proc(500, age_sec=10 * 3600, agent_key="remote-control:brain-v42")
    older = _proc(501, age_sec=20 * 3600, agent_key="remote-control:brain-v42")
    reaped = select_stale_mcp_sessions([newest, older])
    assert reaped == [older]  # newest 10h kept, older 20h reaped


def test_singleton_under_max_age_is_protected() -> None:
    """A lone server younger than the absolute cap is the active session."""
    recent = _proc(600, age_sec=40 * 3600, agent_key="remote-control:refondrre")
    assert select_stale_mcp_sessions([recent]) == []


def test_session_older_than_max_age_is_reaped_even_if_newest() -> None:
    """Owner policy: ANY session older than max_age_sec is cut, even the only one."""
    ancient = _proc(600, age_sec=50 * 3600, agent_key="remote-control:refondrre")
    assert select_stale_mcp_sessions([ancient]) == [ancient]


def test_max_age_default_is_48h() -> None:
    """The default absolute cap is 48h: 47h survives, 49h is cut."""
    just_under = _proc(10, age_sec=47 * 3600, agent_key="a")
    just_over = _proc(11, age_sec=49 * 3600, agent_key="b")
    reaped = select_stale_mcp_sessions([just_under, just_over])
    assert reaped == [just_over]


def test_max_age_none_disables_absolute_cap() -> None:
    """With max_age_sec=None, the newest/only server is never reaped on age."""
    ancient = _proc(700, age_sec=100 * 3600, agent_key="shrik-worker:1")
    assert select_stale_mcp_sessions([ancient], max_age_sec=None) == []


def test_equal_age_siblings_protect_higher_pid_deterministically() -> None:
    """On an age tie (etimes is whole seconds), the higher PID is 'newest'.

    This keeps the protected session stable across runs regardless of ps output
    ordering -- otherwise the protected/reaped choice could flip and kill the
    active session.
    """
    low_pid = _proc(100, age_sec=2 * 3600, agent_key="remote-control:brain-v42")
    high_pid = _proc(200, age_sec=2 * 3600, agent_key="remote-control:brain-v42")
    # input order must not matter
    assert select_stale_mcp_sessions([low_pid, high_pid]) == [low_pid]
    assert select_stale_mcp_sessions([high_pid, low_pid]) == [low_pid]


def test_result_is_sorted_by_pid_for_determinism() -> None:
    """Reaped list is deterministic (sorted by pid) regardless of input order."""
    p_a = _proc(900, age_sec=4 * 3600, agent_key="a")
    p_b = _proc(800, age_sec=4 * 3600, agent_key="b")
    keep_a = _proc(901, age_sec=10 * 60, agent_key="a")
    keep_b = _proc(801, age_sec=10 * 60, agent_key="b")
    reaped = select_stale_mcp_sessions([p_a, p_b, keep_a, keep_b])
    assert [p.pid for p in reaped] == [800, 900]


# ---------------------------------------------------------------------------
# Task 4.1 — Reaper carve-out for the --http-server long-lived process (C3)
# ---------------------------------------------------------------------------

from brain_v42.maintenance.reap_stale_mcp import (  # noqa: E402
    build_mcp_server_procs,
    is_brain_server_cmd,
)


def test_is_brain_server_cmd_excludes_http_server() -> None:
    """The long-lived HTTP server (--http-server) is NEVER a reap candidate.

    Regression guard: the plain stdio form must still match (True).
    """
    # The HTTP server sentinel must cause the gate to return False.
    assert is_brain_server_cmd("/x/.venv/bin/python -m brain_v42.mcp.server --http-server") is False
    # Plain stdio form must still be recognised (regression guard).
    assert is_brain_server_cmd("/x/.venv/bin/python -m brain_v42.mcp.server") is True


def test_build_mcp_server_procs_excludes_http_server_from_snapshot() -> None:
    """A snapshot with both --http-server and a normal stdio process returns only the stdio one.

    The HTTP server process is supervised by systemd and must NEVER enter the
    reap pipeline, even when it has crossed the 48h absolute cap.
    """
    snap = {
        # Long-lived HTTP server (age > 48h) — must be excluded entirely.
        1001: (1, "/x/.venv/bin/python -m brain_v42.mcp.server --http-server"),
        # Normal stdio server for a remote-control agent (age < 48h) — must be kept.
        1002: (1, "/x/.venv/bin/python -m brain_v42.mcp.server"),
    }
    ages = {1001: 50 * 3600.0, 1002: 2 * 3600.0}
    procs = build_mcp_server_procs(snap, ages)
    pids = {p.pid for p in procs}
    assert 1001 not in pids, "http-server process must not enter the reap pipeline"
    assert 1002 in pids, "stdio process must still be a reap candidate"
