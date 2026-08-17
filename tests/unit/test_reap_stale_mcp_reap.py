"""Tests for the reap orchestration (plan + signal), with an injected killer.

``plan_and_reap`` is the testable seam between pure selection and real
``os.kill``: it takes raw ps lines and a ``killer`` callable so we can assert
exactly which signals would be sent without touching real processes.
"""

from __future__ import annotations

from brain_v42.maintenance.reap_stale_mcp import (
    collect_self_and_ancestor_pids,
    plan_and_reap,
)

# Two sessions for agent brain-v42: an old leaked one (to reap) + the active one.
PS_LINES = [
    "    PID    PPID   ELAPSED COMMAND",
    "      5914       1   999999 python3 -m shrik.b",
    "     39859    5914   999999 claude remote-control --name brain-v42",
    # active session (fresh)
    "    700000   39859      120 claude --print --sdk-url https://x",
    "    700001  700000      100 /red/brain_v42/.venv/bin/python -m brain_v42.mcp.server",
    # leaked old session for the SAME agent (5h old)
    "    600000   39859    18000 claude --print --sdk-url https://x",
    "    600001  600000    18000 /red/brain_v42/.venv/bin/python -m brain_v42.mcp.server",
]


def _collect_killer():
    calls: list[tuple[int, int]] = []

    def killer(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    return calls, killer


def test_dry_run_never_calls_killer_but_reports_targets() -> None:
    calls, killer = _collect_killer()
    targets = plan_and_reap(PS_LINES, execute=False, killer=killer)
    assert calls == []  # nothing signalled
    assert {t.pid for t in targets} == {600001}  # only the leaked server


def test_execute_signals_server_and_its_print_parent() -> None:
    import signal

    calls, killer = _collect_killer()
    plan_and_reap(PS_LINES, execute=True, killer=killer)
    signalled = {pid for pid, _sig in calls}
    # the leaked brain server AND its claude --print parent get SIGTERM
    assert signalled == {600001, 600000}
    assert all(sig == signal.SIGTERM for _pid, sig in calls)


def test_active_session_is_never_signalled() -> None:
    calls, killer = _collect_killer()
    plan_and_reap(PS_LINES, execute=True, killer=killer)
    signalled = {pid for pid, _sig in calls}
    assert 700001 not in signalled  # active brain server
    assert 700000 not in signalled  # active --print parent


def test_exclude_pids_protects_caller() -> None:
    calls, killer = _collect_killer()
    targets = plan_and_reap(PS_LINES, execute=True, killer=killer, exclude_pids={600001})
    assert targets == []  # excluded -> nothing to do
    assert calls == []


def test_collect_self_and_ancestor_pids_returns_full_chain() -> None:
    snap = {
        100: (1, "init-ish"),
        101: (100, "wrapper.sh"),
        102: (101, "python -m brain_v42.maintenance.reap_stale_mcp"),
    }
    assert collect_self_and_ancestor_pids(102, snap) == {102, 101, 100}


def test_protect_pid_excludes_full_ancestor_chain() -> None:
    """A reapable older sibling that is an ancestor of the reaper is spared."""
    # reaper runs as a child of the OLD leaked server 600001
    lines = PS_LINES + [
        "        800  600001        5 /red/brain_v42/.venv/bin/python -m brain_v42.maintenance.reap_stale_mcp",
    ]
    calls, killer = _collect_killer()
    # without protection the old sibling would be reaped...
    assert {t.pid for t in plan_and_reap(lines, execute=False, killer=killer)} == {600001}
    # ...but protecting the reaper's chain (which includes 600001) spares it
    protected = plan_and_reap(lines, execute=False, killer=killer, protect_pid=800)
    assert protected == []


def test_parent_print_pid_in_protect_chain_is_not_signalled() -> None:
    """A protected pid is never signalled even when it is a target's --print parent."""
    import signal

    # reaper runs as a child of the OLD session's --print (600000), not the server
    lines = PS_LINES + [
        "        800  600000        5 /red/brain_v42/.venv/bin/python -m brain_v42.maintenance.reap_stale_mcp",
    ]
    calls, killer = _collect_killer()
    plan_and_reap(lines, execute=True, killer=killer, protect_pid=800)
    signalled = {pid for pid, _sig in calls}
    assert 600001 in signalled  # the leaked server is still reaped
    assert 600000 not in signalled  # but its --print parent is protected (reaper's ancestor)
    assert all(sig == signal.SIGTERM for _pid, sig in calls)
