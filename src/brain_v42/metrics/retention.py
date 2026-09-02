"""Single source of truth for ``process_metrics``' residency window.

The table carries one row per ``agent_name`` (that is its primary key), refreshed on
every flush and deleted when it stops being refreshed. Two questions follow, and they
must receive the SAME answer:

* how long do we keep a row? (the purge, in ``flusher`` and ``runtime``)
* how long do we show it? (the read, in ``collector_db``)

On 2026-08-10 they diverged by a factor of 60 — purge at 1 h, read at 60 s — and two of
five real callers were therefore invisible on the panel while their rows existed in the
database. The fix is not to widen one literal but to remove the literals: three constants
that must agree with nothing linking them always end up diverging.

The purge strikes INACTIVITY, not age: a refreshed row stays indefinitely. The window
below therefore bounds an agent's SILENCE, not its row's lifetime.
"""

from __future__ import annotations

PROCESS_METRICS_RETENTION_SECONDS = 3600
"""Silence tolerated before an agent leaves the table and the panel (1 hour)."""

PROCESS_METRICS_FRESH_SQL = (
    f"updated_at > NOW() - INTERVAL '{PROCESS_METRICS_RETENTION_SECONDS} seconds'"
)
"""READ predicate: the agents the panel still shows."""

PROCESS_METRICS_STALE_SQL = (
    f"updated_at < NOW() - INTERVAL '{PROCESS_METRICS_RETENTION_SECONDS} seconds'"
)
"""Prédicat de PURGE : strict complément du précédent, par construction."""

PROCESS_METRICS_LIVE_SECONDS = 60
"""Silence beyond which a PROCESS stops being counted as alive.

Two windows, two questions, and confusing them is exactly the original defect. "Which
agents do we show?" is answered on retention — an agent that went quiet ten minutes ago
did work and must stay on the panel. "How many processes are running?" is answered only
on a short silence: the flush is periodic, so a live process necessarily rewrites its
row. Measured on 2026-08-10: pid 1082528 was inside the one-hour window and absent from
``ps`` — on retention alone, it would have been "active".
"""

PROCESS_METRICS_IS_LIVE_SQL = (
    f"(updated_at > NOW() - INTERVAL '{PROCESS_METRICS_LIVE_SECONDS} seconds')"
)
"""Liveness computed by PostgreSQL.

Deliberately not in Python: ``updated_at`` comes from the database, the sidecar has its
own clock, and a gap between the two would drift in production without ever failing
loudly. One clock decides.
"""
