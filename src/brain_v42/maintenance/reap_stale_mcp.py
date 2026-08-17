"""Reap stale brain MCP server sessions to prevent PG connection saturation.

Background
----------
brain_v42's MCP server runs as one stdio child process per Claude Code session,
each holding a SQLAlchemy/asyncpg pool (``pool_size=5``). red-shrik orchestrates
a fleet of persistent agents via ``claude remote-control --name <agent>``; every
invocation spawns a fresh ``claude --print`` session -> a new brain MCP server ->
~5 PG connections. Old ``--print`` sessions linger after their turn instead of
exiting, so servers accumulate over days until PostgreSQL ``max_connections``
(100) is exhausted and every new connector gets ``FATAL: too many clients``.

The May 2026 three-layer shutdown fix (PDEATHSIG + signal handlers +
``dispose_engine`` in ``finally``) guarantees a server cleans up *when its parent
dies* -- but nothing was killing the abandoned ``--print`` parents. This reaper
is that missing piece: a periodic, conservative sweep that terminates abandoned
sessions while never touching the active one.

Selection rule
--------------
Group servers by their owning agent (the ``remote-control --name`` ancestor, or
the shrik worker pid for shrik-direct connections). Within each agent group the
NEWEST server is the active session and is protected -- EXCEPT past the absolute
cap ``max_age_sec`` (owner policy: any session older than 48h is cut even if it
is the only/newest one, since no legitimate session runs that long). Older
siblings are reaped once they exceed ``min_age_sec``. The reaper's own process
tree is always protected (``plan_and_reap`` ``protect_pid``), so the cap never
reaps the reaper's own session.

Known limitation: under the cap, the heuristic assumes the newest server is the
active one. If a user keeps working in an OLDER session while a newer one exists
for the same agent, the older could be reaped. This is acceptable for an
automated agent fleet and mitigated by ``min_age_sec`` and the dry-run default.

Only the pure :func:`select_stale_mcp_sessions` is exercised by unit tests; the
process discovery and signalling live in the (I/O) CLI layer below.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

# A process is a brain MCP server iff it actually runs the server MODULE -- i.e.
# ``python[ver] -m brain_v42.mcp.server``. A bare substring match would also flag
# a ``grep``/``tail``/editor that merely mentions the module (and, worse, a young
# look-alike could be picked as the "newest" session, getting the REAL active
# server reaped).
_BRAIN_SERVER_RE = re.compile(r"\bpython[0-9.]*\s+-m\s+brain_v42\.mcp\.server(?:\s|$)")
# ``claude remote-control --name <agent>`` identifies the owning agent.
_REMOTE_CONTROL_RE = re.compile(r"remote-control\s+--name\s+(\S+)")
# A genuine ``claude --print`` session: the ``--print`` standalone flag (NOT a
# substring like ``--print-debug``) on a claude command.
_PRINT_FLAG_RE = re.compile(r"(?:^|\s)--print(?:\s|$)")
# Max parent hops to climb when resolving an agent (guards against cycles).
_MAX_ANCESTOR_DEPTH = 12


def is_brain_server_cmd(cmd: str) -> bool:
    """True iff ``cmd`` is a real ``python -m brain_v42.mcp.server`` invocation.

    The long-lived HTTP server (--http-server) is EXCLUDED: it is supervised by
    systemd and must NEVER be reaped (reaping it would flap the whole fleet's brain
    every 15 min once it crosses the 48h cap). See spec C3.
    """
    if "--http-server" in cmd:
        return False
    return _BRAIN_SERVER_RE.search(cmd) is not None


def _is_claude_print(cmd: str) -> bool:
    """True iff ``cmd`` is a genuine ``claude --print`` SDK session."""
    return "claude" in cmd and _PRINT_FLAG_RE.search(cmd) is not None


@dataclass(frozen=True)
class McpServerProc:
    """A running brain MCP server process and the context needed to judge it.

    Attributes:
        pid: The brain MCP server process id.
        age_sec: Wall-clock age of the process in seconds.
        agent_key: Stable key for the owning agent. Servers sharing an
            ``agent_key`` are siblings competing for "active session" status
            (e.g. ``"remote-control:brain-v42"`` or ``"shrik-worker:3747833"``).
        parent_print_pid: The ``claude --print`` parent pid, if any, so the CLI
            layer can fully terminate the abandoned session (not just its MCP
            child). ``None`` for shrik-direct servers whose parent must be left
            alive.
    """

    pid: int
    age_sec: float
    agent_key: str
    parent_print_pid: int | None = None


def select_stale_mcp_sessions(
    procs: Iterable[McpServerProc],
    *,
    min_age_sec: float = 1800.0,
    max_age_sec: float | None = 172800.0,
) -> list[McpServerProc]:
    """Return the subset of ``procs`` that should be reaped.

    Within each agent group the NEWEST server (smallest age; PID breaks ties,
    higher = newer) is the active session and is protected -- EXCEPT past the
    absolute cap ``max_age_sec``: any session older than that is cut even if it
    is the only/newest one of its agent (owner policy -- no legitimate session
    runs ~48h). Older siblings are reaped once they exceed ``min_age_sec``.

    The reaper's own process tree is protected separately (``plan_and_reap``'s
    ``protect_pid``), so the absolute cap never reaps the reaper's own session.

    Args:
        procs: All currently-running brain MCP server processes.
        min_age_sec: Never reap anything younger than this (default 30 min).
            Protects fresh work and brief handoff overlaps.
        max_age_sec: Absolute cap (default 48 h); a session older than this is
            reaped even when it is the newest of its agent. ``None`` disables the
            cap (then the newest of every agent is always protected).

    Returns:
        Servers to reap, sorted by pid for deterministic behaviour.
    """
    groups: dict[str, list[McpServerProc]] = defaultdict(list)
    for proc in procs:
        groups[proc.agent_key].append(proc)

    reap: list[McpServerProc] = []
    for members in groups.values():
        # Stable "newest": smallest age, higher PID wins ties (spawned later).
        newest = min(members, key=lambda m: (m.age_sec, -m.pid))
        for member in members:
            over_cap = max_age_sec is not None and member.age_sec >= max_age_sec
            if over_cap:
                reap.append(member)  # absolute cap: cut even the active session
                continue
            if member is newest:
                continue  # active session -> protected (under the cap)
            if member.age_sec < min_age_sec:
                continue  # too young -> protected (handoff / fresh work)
            reap.append(member)

    return sorted(reap, key=lambda m: m.pid)


# --------------------------------------------------------------------------- #
# Process discovery (I/O-adjacent but kept pure: callers pass in ps output).  #
# --------------------------------------------------------------------------- #

# A ps snapshot maps pid -> (ppid, command line).
PsSnapshot = dict[int, tuple[int, str]]


def parse_ps_snapshot(lines: Iterable[str]) -> tuple[PsSnapshot, dict[int, float]]:
    """Parse ``ps -eo pid,ppid,etimes,args`` output.

    Args:
        lines: Raw output lines (the header line is skipped automatically).

    Returns:
        ``(snapshot, ages)`` where ``snapshot[pid] = (ppid, cmdline)`` and
        ``ages[pid]`` is the process age in seconds. Unparseable lines are
        skipped.
    """
    snapshot: PsSnapshot = {}
    ages: dict[int, float] = {}
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etimes_s, cmd = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
            age = float(etimes_s)
        except ValueError:
            continue  # header row or malformed line
        snapshot[pid] = (ppid, cmd)
        ages[pid] = age
    return snapshot, ages


def _ancestors(pid: int, snapshot: PsSnapshot) -> list[tuple[int, str]]:
    """Return ``(ancestor_pid, ancestor_cmd)`` from immediate parent upward."""
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    cur = pid
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cur is None or cur <= 1 or cur in seen:
            break
        seen.add(cur)
        entry = snapshot.get(cur)
        if entry is None:
            break
        ppid = entry[0]
        pcmd = snapshot.get(ppid, (0, ""))[1]
        chain.append((ppid, pcmd))
        cur = ppid
    return chain


def resolve_agent_key(server_pid: int, snapshot: PsSnapshot) -> tuple[str, int | None]:
    """Resolve a brain MCP server to its owning agent and ``--print`` parent.

    Walks the parent chain and returns ``(agent_key, parent_print_pid)``:

    - ``remote-control:<name>`` when a ``claude remote-control --name`` ancestor
      exists (the common red-shrik fleet case); ``parent_print_pid`` is the
      ``claude --print`` parent so the session can be fully terminated.
    - ``shrik-worker:<ppid>`` when the immediate parent is a shrik process with
      no claude layer; ``parent_print_pid`` is ``None`` (never kill the worker).
    - ``parent:<ppid>`` fallback for anything else.
    """
    chain = _ancestors(server_pid, snapshot)
    if not chain:
        return f"pid:{server_pid}", None

    first_ppid, first_cmd = chain[0]
    print_pid = first_ppid if _is_claude_print(first_cmd) else None

    for _anc_pid, anc_cmd in chain:
        match = _REMOTE_CONTROL_RE.search(anc_cmd)
        if match:
            return f"remote-control:{match.group(1)}", print_pid

    if "shrik" in first_cmd:
        return f"shrik-worker:{first_ppid}", None
    return f"parent:{first_ppid}", print_pid


def build_mcp_server_procs(snapshot: PsSnapshot, ages: dict[int, float]) -> list[McpServerProc]:
    """Build :class:`McpServerProc` records for every brain MCP server in a snapshot."""
    procs: list[McpServerProc] = []
    for pid, (_ppid, cmd) in snapshot.items():
        if not is_brain_server_cmd(cmd):
            continue
        agent_key, print_pid = resolve_agent_key(pid, snapshot)
        procs.append(
            McpServerProc(
                pid=pid,
                age_sec=ages.get(pid, 0.0),
                agent_key=agent_key,
                parent_print_pid=print_pid,
            )
        )
    return procs


# --------------------------------------------------------------------------- #
# Reap orchestration + CLI.                                                    #
# --------------------------------------------------------------------------- #

_PS_ARGV = ["ps", "-eo", "pid,ppid,etimes,args"]


def collect_self_and_ancestor_pids(pid: int, snapshot: PsSnapshot) -> set[int]:
    """Return ``pid`` plus its full ancestor chain (so the reaper never self-harms).

    Computed from the ps snapshot (not ``os.getppid``, which only yields the
    immediate parent) so every layer between the reaper and init is protected.
    """
    pids = {pid}
    for ancestor_pid, _cmd in _ancestors(pid, snapshot):
        if ancestor_pid > 1:  # init is never a meaningful thing to protect
            pids.add(ancestor_pid)
    return pids


def plan_and_reap(
    ps_lines: Iterable[str],
    *,
    execute: bool,
    killer: Callable[[int, int], None],
    min_age_sec: float = 1800.0,
    max_age_sec: float | None = 172800.0,
    exclude_pids: set[int] | None = None,
    protect_pid: int | None = None,
) -> list[McpServerProc]:
    """Select stale sessions from ``ps`` output and (optionally) terminate them.

    When ``execute`` is true, each target's brain server receives ``SIGTERM``
    (the May-2026 shutdown fix turns that into a graceful ``dispose_engine``
    that releases the asyncpg pool), and its ``claude --print`` parent receives
    ``SIGTERM`` too so the abandoned session is fully gone. shrik-direct servers
    (no ``--print`` parent) only signal the server, never the shrik worker.

    Args:
        ps_lines: Output lines of ``ps -eo pid,ppid,etimes,args``.
        execute: If false (the safe default for new installs), nothing is
            signalled -- targets are only returned/logged (dry run).
        killer: Callable ``(pid, signal)`` used to send signals (injected for
            tests; ``os.kill`` in production).
        min_age_sec: Never reap anything younger than this.
        max_age_sec: Absolute cap; a session older than this is reaped even if it
            is its agent's newest/only one (``None`` disables the cap).
        exclude_pids: Pids to protect unconditionally -- never reaped as a target
            and never signalled as another target's ``--print`` parent.
        protect_pid: If given, this pid and its entire ancestor chain are added
            to the protected set (the reaper protecting its own process tree).

    Returns:
        The selected (and, if executing, signalled) targets.
    """
    snapshot, ages = parse_ps_snapshot(ps_lines)
    protect = set(exclude_pids or set())
    if protect_pid is not None:
        protect |= collect_self_and_ancestor_pids(protect_pid, snapshot)

    procs = build_mcp_server_procs(snapshot, ages)
    selected = select_stale_mcp_sessions(procs, min_age_sec=min_age_sec, max_age_sec=max_age_sec)
    targets = [p for p in selected if p.pid not in protect]

    for target in targets:
        logger.info(
            "reap_stale_mcp.target",
            pid=target.pid,
            agent=target.agent_key,
            age_sec=round(target.age_sec),
            parent_print_pid=target.parent_print_pid,
            execute=execute,
        )
        if not execute:
            continue
        _safe_kill(killer, target.pid)
        if target.parent_print_pid is not None and target.parent_print_pid not in protect:
            _safe_kill(killer, target.parent_print_pid)

    return targets


def _safe_kill(killer: Callable[[int, int], None], pid: int) -> None:
    """Send SIGTERM, swallowing ProcessLookupError (already gone) only."""
    try:
        killer(pid, signal.SIGTERM)
    except ProcessLookupError:
        logger.info("reap_stale_mcp.already_gone", pid=pid)


def _read_ps() -> list[str]:
    out = subprocess.run(_PS_ARGV, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Dry-run by default; pass ``--execute`` to actually reap."""
    parser = argparse.ArgumentParser(
        description="Reap stale brain MCP server sessions (PG-connection guardrail).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send signals. Without this flag the reaper is a dry run.",
    )
    parser.add_argument(
        "--min-age-min",
        type=float,
        default=30.0,
        help="Never reap sessions younger than this many minutes (default 30).",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=48.0,
        help="Cut ANY session older than this, even an agent's active one "
        "(default 48; 0 disables the absolute cap).",
    )
    args = parser.parse_args(argv)

    max_age_sec = None if args.max_age_hours <= 0 else args.max_age_hours * 3600.0
    targets = plan_and_reap(
        _read_ps(),
        execute=args.execute,
        killer=os.kill,
        min_age_sec=args.min_age_min * 60.0,
        max_age_sec=max_age_sec,
        protect_pid=os.getpid(),
    )

    verb = "reaped" if args.execute else "would reap"
    print(
        f"reap_stale_mcp: {verb} {len(targets)} stale brain MCP session(s): "
        f"{[t.pid for t in targets]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
